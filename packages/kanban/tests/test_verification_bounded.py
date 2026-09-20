"""Verification boundary — bounded capture, process-group kill, full-argv
allowlist, committed+dirty diff, allowed-scope enforcement, sanitization.

Parent-identified defects:
- ``capture_output=True`` buffered output unbounded and the timeout killed
  only the leader, leaking stubborn children.
- Only argv[0]'s basename was allowlisted — ``python -c <anything>`` ran.
- ``capture_diff`` omitted dirty/untracked changes whenever the commit diff
  was non-empty.
- Changed files outside ``allowed_scope`` were never checked.
- Evidence (argv/rc/output/diff) was not persisted durably or sanitized.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cli_provider_kanban.dispatch import dispatch_once
from cli_provider_kanban.store import DispatchStore
from cli_provider_kanban.worktree import (
    WorktreeError,
    capture_diff,
    changed_files,
    check_scope,
    prepare_worktree,
    remove_worktree,
    run_verification,
    sanitize_text,
)

from conftest import (  # noqa: F401
    FakeWrapper,
    dispatch_policy,
    git_repo,
    requires_hermes,
    write_policy,
)
from test_dispatch import HERMES_ENV_KEYS, _spec_for, board  # noqa: F401

pytestmark = requires_hermes

PY = sys.executable
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def _wt(git_repo, tmp_path):
    repo, base = git_repo
    return repo, prepare_worktree(
        repo=repo, worktree_root=tmp_path / "wt",
        workspace_id="ws", dispatch_id="d_vb", base_revision=base,
    )


class TestBoundedRun:
    def test_timeout_kills_process_group(self, git_repo, tmp_path):
        """A stubborn child in the same process group dies with the run —
        the leader-only kill leaked it."""
        repo, wt = _wt(git_repo, tmp_path)
        pid_file = tmp_path / "child.pid"
        argv = [
            PY, "-c",
            "import subprocess,sys,os;"
            "c=subprocess.Popen(['sleep','60'],"
            " stdout=subprocess.DEVNULL);"
            f"open({str(pid_file)!r},'w').write(str(c.pid));"
            "import time;time.sleep(60)",
        ]
        res = run_verification(
            wt, argv,
            executables={"python": PY, "python3": PY},
            commands=[[PY, "-c", "*"]],
            timeout_seconds=1.0, max_output_bytes=8192,
        )
        assert not res.ok and res.timed_out
        child_pid = int(pid_file.read_text())
        # The whole group got SIGKILL — the child is gone (or a zombie
        # reaped by init); either way it must not still run 'sleep'.
        try:
            out = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child_pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception:
            out = ""
        assert not out or out.startswith("Z")
        remove_worktree(wt, repo)

    def test_timeout_returns_partial_output(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        argv = [PY, "-c",
                "import sys;sys.stdout.write('PARTIAL-MARKER\\n');"
                "sys.stdout.flush();import time;time.sleep(60)"]
        res = run_verification(
            wt, argv,
            executables={}, commands=[[PY, "-c", "*"]],
            timeout_seconds=1.0, max_output_bytes=8192,
        )
        assert not res.ok and res.timed_out
        assert "PARTIAL-MARKER" in res.output
        remove_worktree(wt, repo)

    def test_output_is_capped(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        argv = [PY, "-c", "import sys;sys.stdout.write('x'*10_000_000)"]
        start = time.time()
        res = run_verification(
            wt, argv,
            executables={}, commands=[[PY, "-c", "*"]],
            timeout_seconds=30, max_output_bytes=4096,
        )
        assert time.time() - start < 25  # no OOM hang on the 10MB buffer
        assert res.truncated
        assert len(res.output.encode()) <= 4096 + 64
        remove_worktree(wt, repo)

    def test_full_argv_must_match_not_basename(self, git_repo, tmp_path):
        """Basename allowlisting let ``python -c <arbitrary>`` through; the
        commands allowlist pins the full operator-trusted argv."""
        repo, wt = _wt(git_repo, tmp_path)
        res = run_verification(
            wt, ["python", "-c", "import os;print(os.getuid())"],
            executables={"python": PY},
            commands=[["python", "--version"]],   # only --version allowed
            timeout_seconds=10, max_output_bytes=1024,
        )
        assert not res.ok and res.exit_code is None
        assert "allowlist" in res.output or "not allowed" in res.output
        remove_worktree(wt, repo)

    def test_commands_wildcard_tail(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        res = run_verification(
            wt, ["echo", "hello", "world"],
            executables={"echo": "/usr/bin/echo"},
            commands=[["echo", "*"]],
            timeout_seconds=10, max_output_bytes=1024,
        )
        assert res.ok and "hello world" in res.output
        remove_worktree(wt, repo)

    def test_no_commands_means_nothing_runs(self, git_repo, tmp_path):
        """An empty command allowlist denies every argv — fail closed."""
        repo, wt = _wt(git_repo, tmp_path)
        res = run_verification(
            wt, ["true"],
            executables={"true": "/usr/bin/true"},
            commands=[],
            timeout_seconds=10, max_output_bytes=1024,
        )
        assert not res.ok and res.exit_code is None
        remove_worktree(wt, repo)


class TestDiffAndScope:
    def test_committed_and_dirty_changes_both_captured(
        self, git_repo, tmp_path
    ):
        repo, base = git_repo
        wt = prepare_worktree(
            repo=repo, worktree_root=tmp_path / "wt",
            workspace_id="ws", dispatch_id="d_diff2", base_revision=base,
        )
        # A committed change on the jev branch...
        (wt.path / "committed.txt").write_text("committed\n")
        subprocess.run(["git", "add", "-A"], cwd=wt.path, env=ENV,
                       check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "c"], cwd=wt.path, env=ENV,
                       check=True, capture_output=True)
        # ...PLUS a dirty tracked edit and an untracked file.
        (wt.path / "seed.txt").write_text("dirty\n")
        (wt.path / "untracked.txt").write_text("untracked\n")
        diff = capture_diff(wt, max_bytes=65536)
        assert "committed.txt" in diff
        assert "dirty" in diff                    # tracked dirty edit
        assert "untracked.txt" in diff            # untracked via add -N
        files = changed_files(wt)
        assert {"committed.txt", "seed.txt", "untracked.txt"} <= set(files)
        remove_worktree(wt, repo)

    def test_out_of_scope_change_detected(self):
        violations = check_scope(
            ["packages/kanban/x.py", "docs/SECRET.md"],
            ["packages/kanban/"],
        )
        assert violations == ["docs/SECRET.md"]

    def test_scope_globs(self):
        assert check_scope(["a/b.py"], ["*.py"]) == []
        assert check_scope(["a/b.py"], ["a/*.py"]) == []
        assert check_scope(["a/b/c.txt"], ["a/**"]) == []

    def test_natural_language_scope_fails_closed(self):
        """A scope string that is neither a clean relpath nor a glob cannot
        be enforced — any change then violates it (no guessing parser)."""
        violations = check_scope(
            ["x.py"], ["only files under packages/kanban"],
        )
        assert violations == ["x.py"]


class TestSanitize:
    def test_bearer_token_redacted(self):
        out = sanitize_text("Authorization: Bearer TOPSECRET123", ["TOPSECRET123"])
        assert "TOPSECRET123" not in out
        assert "REDACTED" in out

    def test_known_secret_value_redacted(self):
        out = sanitize_text("token=abc123 more text", ["abc123"])
        assert "abc123" not in out


class TestDispatchEvidence:
    """Durable evidence: the receipt records sanitized argv/rc/output,
    artifact hashes, and a bounded diff file — not just 'passed'."""

    def _card(self, bridge, tmp_path, data, rev, **spec_over):
        tid = bridge.call("create_task", title="c", assignee="jev-native",
                          body="x")["task_id"]
        spec = _spec_for(tid, rev, **spec_over)
        del spec["task_id"]
        tm = tmp_path / f"tm_{tid}.json"
        tm.write_text(json.dumps({tid: spec}))
        data["task_map"] = str(tm)
        return tid

    def test_evidence_recorded_on_review(self, board, tmp_path,
                                       dispatch_policy):
        db, bridge = board
        data, rev = dispatch_policy
        tid = self._card(bridge, tmp_path, data, rev,
                         verification={
                             "argv": ["echo", "verify-ok"],
                             "criteria": "exit 0"})
        policy_path = write_policy(tmp_path, data)
        store_path = tmp_path / "dispatch.db"
        report = dispatch_once(board_db=db, policy_path=policy_path,
                               store_path=store_path, client=FakeWrapper())
        rec = next(r for r in report["results"] if r["task_id"] == tid)
        assert rec["action"] == "review"
        receipt = DispatchStore(store_path).for_task(tid)[0]
        ev = receipt.evidence
        assert ev is not None
        assert ev["verification"]["argv"] == ["echo", "verify-ok"]
        assert ev["verification"]["exit_code"] == 0
        assert "verify-ok" in ev["verification"]["output"]
        assert ev["diff"]["sha256"]
        assert Path(ev["diff"]["path"]).is_file()

    def test_out_of_scope_change_is_quality_failure(
        self, board, tmp_path, dispatch_policy
    ):
        db, bridge = board
        data, rev = dispatch_policy
        ws = data["workspaces"]["ws-main"]
        # The "run" leaves a file outside the card's allowed_scope.
        prepared = Path(ws["prepared_worktree"])
        tid = self._card(bridge, tmp_path, data, rev,
                         verification={
                             "argv": ["touch", "evil.out"],
                             "criteria": "exit 0"},
                         allowed_scope=["packages/kanban/"])
        policy_path = write_policy(tmp_path, data)
        report = dispatch_once(board_db=db, policy_path=policy_path,
                               store_path=tmp_path / "dispatch.db",
                               client=FakeWrapper())
        rec = next(r for r in report["results"] if r["task_id"] == tid)
        assert rec["action"] == "blocked"
        assert "quality_failed" in rec["reason"] or "scope" in rec["reason"]
        (prepared / "evil.out").unlink(missing_ok=True)
