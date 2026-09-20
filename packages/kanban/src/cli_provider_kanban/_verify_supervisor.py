"""One-shot verification supervisor — runs as a SEPARATE helper process.

Spawned as ``[sys.executable, this_file, spec_path]`` by
``run_verification``. It makes itself a child subreaper
(``PR_SET_CHILD_SUBREAPER``, this process only — never a global flag on the
dispatcher), then launches the allowlisted leader argv with a scrubbed
environment and ``stdin`` closed. After the leader exits for ANY reason —
success, failure, timeout — it deterministically finds every descendant by
walking ``/proc`` ppid ancestry back to itself (this catches same-group
children, ``setsid`` escapees and double-forked orphans, because the
subreaper flag makes orphans reparent HERE instead of to init) and kills
them within a bounded reap budget.

The result is a single JSON report written to an inherited fd so it can
never be interleaved with child stdout/stderr. If the report is missing or
descendants survive the budget, the caller must fail closed — a claimed
"verified clean" is only valid when supervision was actually confirmed.

Residual limits (documented, not hidden): this is process supervision, not
an OS sandbox — a hostile same-UID process can still ptrace/signal our
other processes or write outside the worktree via means outside this
scope; the ancestry scan is per-scan atomic-ish but a same-UID adversary
that wins a PID-reuse race mid-kill could in principle substitute a
process. That is why the caller re-validates tree identity separately and
why scope violations are computed from the filesystem, not from git state
the child influenced.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import signal
import subprocess
import sys
import time

PR_SET_CHILD_SUBREAPER = 36  # Linux prctl op
_ANCESTRY_MAX_DEPTH = 64
_REPORT_CAP = 65536


def _become_subreaper() -> bool:
    """This process becomes the reaper for orphaned descendants.

    Scoped to THIS process — deliberately not set on the dispatcher, which
    would steal unrelated children process-wide.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except (OSError, AttributeError):
        return False
    return libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0


def _proc_stat(pid: int) -> dict | None:
    """Parse /proc/<pid>/stat — comm may contain spaces and parens, so
    fields are taken after the LAST ')'."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    rparen = data.rfind(b")")
    if rparen < 0:
        return None
    try:
        fields = data[rparen + 2:].split()
        return {
            "state": fields[0].decode("ascii", "replace"),
            "ppid": int(fields[1]),
            "pgrp": int(fields[2]),
            "session": int(fields[3]),
        }
    except (IndexError, ValueError):
        return None


def _is_descendant(pid: int, root: int) -> bool:
    """Fresh ppid-ancestry walk: does pid's chain reach ``root``?

    Zombies in the MIDDLE of the chain still carry their real ppid, so the
    walk continues through them; a zombie TARGET is not reported by the
    caller (it is already dead). Chains that reach pid 1 without passing
    ``root`` are not ours — under the subreaper flag no descendant of this
    subtree can reparent past us.
    """
    seen = set()
    cur = pid
    for _ in range(_ANCESTRY_MAX_DEPTH):
        st = _proc_stat(cur)
        if st is None:
            return False
        ppid = st["ppid"]
        if ppid == root:
            return True
        if ppid in (0, 1) or ppid == cur or ppid in seen:
            return False
        seen.add(cur)
        cur = ppid
    return False


def _descendants(root: int) -> set[int]:
    """Live (non-zombie) pids whose ppid-ancestry reaches ``root``."""
    try:
        pids = [
            int(name) for name in os.listdir("/proc") if name.isdigit()
        ]
    except OSError:
        return set()
    out = set()
    for pid in pids:
        if pid in (0, root):
            continue
        st = _proc_stat(pid)
        if st is None or st["state"] == "Z":
            continue
        if _is_descendant(pid, root):
            out.add(pid)
    return out


def _kill_tree(root: int, budget_s: float, *, _kill=os.kill,
               _sleep=time.sleep, _now=time.monotonic) -> list[int]:
    """SIGKILL every live descendant of ``root`` until none remain or the
    budget expires. Returns the sorted pids that survived the budget
    (empty = confirmed cleanup).

    Identity is re-verified by a fresh ancestry walk immediately before
    each signal — no retained pid list and no process-group signal, so a
    reaped leader's recycled pgid can never redirect a signal at an
    unrelated process.
    """
    deadline = _now() + budget_s
    while True:
        desc = _descendants(root)
        if not desc:
            return []
        if _now() >= deadline:
            return sorted(desc)
        for pid in desc:
            try:
                if _is_descendant(pid, root):
                    _kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        _sleep(0.05)


def _reap_children() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        except OSError as exc:
            if exc.errno == errno.ECHILD:
                return
            return
        if pid <= 0:
            return


class _Abort(Exception):
    pass


def _on_term(_sig, _frm):
    raise _Abort()


def _emit(report: dict, fd: int) -> None:
    try:
        blob = json.dumps(report)[:_REPORT_CAP].encode()
        with os.fdopen(fd, "wb", closefd=True) as fh:
            fh.write(blob)
    except OSError:
        pass


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return 2
    report_fd = int(os.environ.get("_JEV_REPORT_FD", "-1"))
    report = {
        "leader_exit": None, "timed_out": False, "spawn_error": None,
        "unsupported": None, "survivors": [], "aborted": False,
    }
    try:
        with open(argv[1], "rb") as fh:
            spec = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        report["spawn_error"] = f"spec unreadable: {type(exc).__name__}"
        if report_fd >= 0:
            _emit(report, report_fd)
        return 2

    if not _become_subreaper():
        report["unsupported"] = (
            "PR_SET_CHILD_SUBREAPER unavailable — descendant cleanup "
            "cannot be confirmed on this host"
        )
        if report_fd >= 0:
            _emit(report, report_fd)
        return 2

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    try:
        leader = subprocess.Popen(
            spec["argv"], cwd=spec["cwd"], env=spec["env"],
            stdin=subprocess.DEVNULL,
            stdout=sys.stdout.fileno(), stderr=sys.stderr.fileno(),
            start_new_session=True,   # own session; group tracked by ancestry
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report["spawn_error"] = f"spawn failed: {type(exc).__name__}"
        if report_fd >= 0:
            _emit(report, report_fd)
        return 2

    deadline_s = float(spec.get("deadline", 60.0))
    reap_budget = float(spec.get("reap", 10.0))
    try:
        try:
            leader.wait(timeout=deadline_s)
        except subprocess.TimeoutExpired:
            report["timed_out"] = True
    except _Abort:
        report["aborted"] = True
        report["timed_out"] = True

    if leader.poll() is None:
        try:
            leader.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        leader.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    report["leader_exit"] = leader.poll()

    _reap_children()
    survivors = _kill_tree(os.getpid(), reap_budget)
    _reap_children()
    # Post-kill verification pass — only non-zombie live descendants count.
    report["survivors"] = survivors
    _emit(report, report_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
