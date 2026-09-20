"""Verification supervision repair proofs (S4) — REAL subprocess fixtures.

The allowlisted argv runs model-authored content, so these tests prove:
descendant cleanup on EVERY exit path (success, failure, timeout,
pipe-holding children, same-group children, setsid/double-fork escapees),
a scrubbed minimal environment, and stale-identity safety — all with
synthetic processes and scoped cleanup, never broad pkill.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cli_provider_kanban import _verify_supervisor as vs
from cli_provider_kanban.worktree import (
    changed_files,
    prepare_worktree,
    remove_worktree,
    run_verification,
)

from conftest import git_repo  # noqa: F401  (fixture)

PY = sys.executable
SH = "/bin/sh"


def _wt(git_repo, tmp_path, dispatch_id="d_vs"):
    repo, base = git_repo
    return repo, prepare_worktree(
        repo=repo, worktree_root=tmp_path / "wt",
        workspace_id="ws", dispatch_id=dispatch_id, base_revision=base,
    )


def _run(wt, argv, timeout=10.0, max_output=8192, commands=None):
    return run_verification(
        wt, argv, executables={},
        commands=commands if commands is not None else [[argv[0], "*"]],
        timeout_seconds=timeout, max_output_bytes=max_output,
    )


def _alive(pid: int) -> bool:
    """Live non-zombie process."""
    try:
        data = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return False
    rparen = data.rfind(b")")
    return rparen >= 0 and data[rparen + 2:rparen + 3] != b"Z"


def _wait_dead(pid: int, timeout: float = 8.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _wait_file(path: Path, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.is_file():
            return True
        time.sleep(0.02)
    return False


class TestBasicOutcomes:
    def test_normal_success(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        res = _run(wt, [PY, "-c", "print('verify-ok')"])
        assert res.ok and res.exit_code == 0
        assert "verify-ok" in res.output
        assert res.cleanup == "confirmed"
        remove_worktree(wt, repo)

    def test_nonzero_exit(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        res = _run(wt, [PY, "-c", "import sys;sys.exit(3)"])
        assert not res.ok and res.exit_code == 3
        remove_worktree(wt, repo)

    def test_timeout(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        start = time.time()
        res = _run(wt, [PY, "-c", "import time;time.sleep(60)"], timeout=1.0)
        assert not res.ok and res.timed_out
        assert time.time() - start < 25
        remove_worktree(wt, repo)

    def test_high_output_bounded(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        start = time.time()
        res = _run(
            wt,
            [PY, "-c",
             "import sys;sys.stdout.write('x'*5_000_000);"
             "sys.stderr.write('y'*5_000_000)"],
            max_output=4096,
        )
        assert time.time() - start < 25
        assert res.truncated
        assert len(res.output.encode()) <= 4096 + 64
        remove_worktree(wt, repo)

    def test_stdin_closed_not_inherited(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        res = _run(wt, [PY, "-c",
                        "import sys;print(len(sys.stdin.read()))"])
        assert res.ok and "0" in res.output
        remove_worktree(wt, repo)


class TestDescendantCleanup:
    """Every exit path must leave no live descendants."""

    def test_success_path_kills_same_group_child(self, git_repo, tmp_path):
        """A same-session child outliving a SUCCESSFUL leader must not
        survive verification — the old code only killpg'd on timeout."""
        repo, wt = _wt(git_repo, tmp_path)
        pid_file = tmp_path / "child.pid"
        argv = [
            PY, "-c",
            "import subprocess,sys;"
            "c=subprocess.Popen(['sleep','60']);"
            f"open({str(pid_file)!r},'w').write(str(c.pid))",
        ]
        start = time.time()
        res = _run(wt, argv)
        assert time.time() - start < 20
        assert res.ok and res.cleanup == "confirmed"
        assert _wait_file(pid_file)
        child = int(pid_file.read_text())
        assert _wait_dead(child), "same-group descendant survived success"
        remove_worktree(wt, repo)

    def test_timeout_kills_child_tree(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        pid_file = tmp_path / "child.pid"
        argv = [
            PY, "-c",
            "import subprocess,time;"
            "c=subprocess.Popen(['sleep','60'],"
            " stdout=subprocess.DEVNULL);"
            f"open({str(pid_file)!r},'w').write(str(c.pid));"
            "time.sleep(60)",
        ]
        res = _run(wt, argv, timeout=1.0)
        assert not res.ok and res.timed_out
        assert _wait_file(pid_file)
        assert _wait_dead(int(pid_file.read_text()))
        remove_worktree(wt, repo)

    def test_setsid_double_fork_delayed_write_killed(
        self, git_repo, tmp_path
    ):
        """setsid + orphaned grandchild escapes a plain killpg — the
        subreaper must adopt and kill it BEFORE its delayed mutation."""
        repo, wt = _wt(git_repo, tmp_path)
        pid_file = tmp_path / "grand.pid"
        marker = wt.path / "LATE_MUTATION.txt"
        argv = [
            SH, "-c",
            "setsid sh -c '"
            f"echo $$ > {pid_file}; "
            "sleep 3; "
            f"echo late > {marker}"
            "' </dev/null >/dev/null 2>&1 & "
            "echo leader-done; exit 0",
        ]
        start = time.time()
        res = _run(wt, argv)
        assert time.time() - start < 20
        assert res.ok and res.cleanup == "confirmed"
        assert _wait_file(pid_file)
        grand = int(pid_file.read_text().strip())
        assert _wait_dead(grand), "setsid grandchild survived"
        # Well past its scheduled write — no post-verification mutation.
        time.sleep(4)
        assert not marker.exists()
        assert "LATE_MUTATION.txt" not in changed_files(wt)
        remove_worktree(wt, repo)

    def test_child_holding_pipes_does_not_hang(self, git_repo, tmp_path):
        """A descendant holding the leader's pipe write-ends must not hang
        the verification past its deadline or leak the pipes."""
        repo, wt = _wt(git_repo, tmp_path)
        pid_file = tmp_path / "hold.pid"
        argv = [
            PY, "-c",
            "import subprocess,sys;"
            # child inherits our stdout/stderr and holds them open
            "c=subprocess.Popen(['sleep','60']);"
            f"open({str(pid_file)!r},'w').write(str(c.pid));"
            "print('leader-done')",
        ]
        start = time.time()
        res = _run(wt, argv)
        assert time.time() - start < 20
        assert res.ok and "leader-done" in res.output
        assert _wait_file(pid_file)
        assert _wait_dead(int(pid_file.read_text()))
        remove_worktree(wt, repo)


class TestEnvironmentScrubbing:
    def test_parent_secrets_not_inherited(self, git_repo, tmp_path,
                                        monkeypatch):
        monkeypatch.setenv("JEV_FAKE_SECRET", "SUPER-SECRET-VALUE")
        monkeypatch.setenv("PYTHONPATH", "/model-controlled")
        monkeypatch.setenv("LD_PRELOAD", "/evil.so")
        repo, wt = _wt(git_repo, tmp_path)
        res = _run(wt, [PY, "-c",
                        "import os;print(sorted(os.environ.items()))"])
        assert res.ok
        assert "SUPER-SECRET-VALUE" not in res.output
        assert "JEV_FAKE_SECRET" not in res.output
        assert "PYTHONPATH" not in res.output
        assert "LD_PRELOAD" not in res.output
        # Positive control: the scrubbed env still carries what it needs.
        assert "PATH" in res.output
        remove_worktree(wt, repo)

    def test_child_home_is_outside_worktree(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        res = _run(wt, [PY, "-c", "import os;print(os.environ['HOME'])"])
        assert res.ok
        home = res.output.strip().splitlines()[-1]
        assert str(wt.path) not in home
        assert home != os.environ.get("HOME", "")
        remove_worktree(wt, repo)

    def test_unsupported_host_refuses_not_claims(
        self, git_repo, tmp_path, monkeypatch
    ):
        from cli_provider_kanban import worktree as wmod
        monkeypatch.setattr(
            wmod, "_supervision_supported",
            lambda: "verification unsupported: simulated non-Linux host",
        )
        repo, wt = _wt(git_repo, tmp_path)
        res = _run(wt, [PY, "-c", "print(1)"])
        assert not res.ok
        assert res.cleanup == "unsupported"
        assert "unsupported" in res.output
        remove_worktree(wt, repo)


class TestStaleIdentitySafety:
    """The reaper must signal only verified-current descendants — never a
    retained/stale identity, and never an unrelated live process."""

    def test_kill_tree_signals_only_true_descendants(self, tmp_path):
        # Orphaned, own-session sleeper — deliberately NOT a descendant of
        # this test process (double-fork reparents it to init, like a
        # model's escapee that a stale retained pgid might later alias).
        pid_file = tmp_path / "orphan.pid"
        subprocess.run(
            [PY, "-c",
             "import os,sys,time;"
             "pid=os.fork();"
             "pid and sys.exit(0);"          # parent exits -> child orphan
             "os.setsid();"
             "p2=os.fork();"
             "p2 and os._exit(0);"          # child exits -> grand orphan
             f"open({str(pid_file)!r},'w').write(str(os.getpid()));"
             "time.sleep(60)"],
            check=True, timeout=10,
        )
        assert _wait_file(pid_file)
        orphan = int(pid_file.read_text())
        assert _alive(orphan)
        assert not vs._is_descendant(orphan, os.getpid())
        # Positive control: a real live descendant IS signalled.
        direct = subprocess.Popen(["sleep", "60"])
        kills: list[int] = []
        try:
            survivors = vs._kill_tree(
                os.getpid(), 1.0, _kill=lambda p, s: kills.append(p),
            )
            assert direct.pid in kills
            assert orphan not in kills
            assert survivors == [direct.pid]  # spy didn't actually kill
        finally:
            direct.kill()
            direct.wait()
            try:
                os.kill(orphan, 9)
            except (ProcessLookupError, PermissionError):
                pass
        assert _wait_dead(orphan)
