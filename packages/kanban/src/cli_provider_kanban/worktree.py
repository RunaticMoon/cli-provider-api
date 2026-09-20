"""Per-card worktree + verification capture.

Every card runs in a fresh git worktree under the trusted workspace's
``worktree_root``, branched deterministically from the pinned
``base_revision`` at ``jev/<dispatch_id>``. Dependencies must prove their
``integrated_revision`` is an ancestor of the base — a ``done`` status alone
is never sufficient. All subprocesses are argv lists; no shell strings.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_SAFE = re.compile(r"[^A-Za-z0-9_-]")


class WorktreeError(Exception):
    """Trusted-workspace or git precondition failure (fail-closed)."""


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    base_revision: str
    workspace_id: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    exit_code: int | None
    output: str            # bounded tail
    truncated: bool
    timed_out: bool = False


@dataclass(frozen=True)
class DepCheck:
    task_id: str
    ok: bool
    reason: str | None = None
    integrated_revision: str | None = None


def _git(repo: Path, *argv: str, check: bool = True,
         capture: bool = True, timeout: float = 60.0) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-C", str(repo), *argv],
        capture_output=capture,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(argv[:2])} failed in {repo}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
        )
    return proc


def validate_commit(rev: str) -> str:
    """Full 40-hex commit only — never a ref, range, or arbitrary string."""
    if not _FULL_COMMIT_RE.match(rev or ""):
        raise WorktreeError(
            f"base/integrated revision {rev!r} is not a full 40-hex commit"
        )
    return rev


def resolve_repo(repo: str) -> Path:
    """The configured repo must be a real git repository (fail-closed)."""
    root = Path(repo).resolve()
    if not root.is_dir():
        raise WorktreeError(f"workspace repo {repo!r} does not exist")
    probe = _git(root, "rev-parse", "--git-dir", check=False)
    if probe.returncode != 0:
        raise WorktreeError(f"workspace repo {repo!r} is not a git repository")
    return root


def assert_is_ancestor(repo: Path, rev: str, base: str) -> None:
    """``rev`` must already be contained in ``base`` (dependency integrated)."""
    proc = _git(
        repo, "merge-base", "--is-ancestor", rev, base, check=False,
    )
    if proc.returncode != 0:
        raise WorktreeError(
            f"integrated revision {rev[:12]} is not an ancestor of "
            f"base {base[:12]}"
        )


def check_dependencies(
    repo: Path,
    base_revision: str,
    deps: list[tuple[str, str | None]],
) -> list[DepCheck]:
    """``deps`` = (task_id, integrated_revision-or-None). Every dependency
    must carry a recorded integrated_revision that is an ancestor of base."""
    out: list[DepCheck] = []
    for task_id, rev in deps:
        if not rev:
            out.append(DepCheck(task_id, False, "no integrated_revision recorded"))
            continue
        try:
            validate_commit(rev)
            assert_is_ancestor(repo, rev, base_revision)
            out.append(DepCheck(task_id, True, integrated_revision=rev))
        except WorktreeError as exc:
            out.append(DepCheck(task_id, False, str(exc), rev))
    return out


def branch_name(dispatch_id: str) -> str:
    return "jev/" + _BRANCH_SAFE.sub("-", dispatch_id)


def prepare_worktree(
    *,
    repo: Path,
    worktree_root: Path,
    workspace_id: str,
    dispatch_id: str,
    base_revision: str,
) -> Worktree:
    """Create the deterministic per-card worktree at the pinned base."""
    validate_commit(base_revision)
    # Confirm the pinned base actually exists in the trusted repo.
    _git(repo, "cat-file", "-e", f"{base_revision}^{{commit}}")

    root = Path(worktree_root)
    root.mkdir(parents=True, exist_ok=True)
    wt_path = (root / dispatch_id).resolve()
    if not str(wt_path).startswith(str(root.resolve()) + os.sep):
        raise WorktreeError(f"worktree path escapes root: {wt_path}")
    branch = branch_name(dispatch_id)
    proc = _git(
        repo,
        "worktree", "add", "--detach", str(wt_path), base_revision,
        check=False,
    )
    if proc.returncode != 0:
        raise WorktreeError(
            f"git worktree add failed: {(proc.stderr or '').strip()[:400]}"
        )
    # Named branch for the card's commits, still at the pinned base.
    _git(wt_path, "checkout", "-B", branch)
    return Worktree(
        path=wt_path, branch=branch, base_revision=base_revision,
        workspace_id=workspace_id,
    )


def _resolve_in_root(root: Path, rel: str) -> Path:
    """Card-declared relative path -> absolute, confined to ``root`` and
    rejecting symlink escapes."""
    if not rel or rel.startswith("/") or rel.startswith("~"):
        raise WorktreeError(f"path {rel!r} must be workspace-relative")
    candidate = (root / rel).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise WorktreeError(
            f"path {rel!r} resolves outside the worktree root"
        )
    return candidate


def capture_diff(wt: Worktree, max_bytes: int) -> str:
    """Bounded ``git diff`` of the worktree against the pinned base —
    includes untracked files via intent-to-add so partial work is captured."""
    _git(wt.path, "add", "-N", ".", check=False, timeout=60)
    proc = _git(
        wt.path, "diff", "--binary", f"{wt.base_revision}..HEAD",
        check=False, timeout=120,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        # No commits yet: diff the dirty tree instead.
        proc = _git(wt.path, "diff", "--binary", wt.base_revision,
                    check=False, timeout=120)
    out = proc.stdout or ""
    if len(out.encode("utf-8", "replace")) > max_bytes:
        out = out.encode("utf-8", "replace")[:max_bytes].decode(
            "utf-8", "replace"
        ) + "\n[diff truncated]\n"
    return out


def collect_artifacts(
    wt: Worktree, artifact_paths: list[str], *, max_bytes: int
) -> tuple[list[Path], list[str]]:
    """Return (collected, missing). Missing artifacts are a typed failure —
    the caller blocks ``quality_failed``, never falls back."""
    collected: list[Path] = []
    missing: list[str] = []
    for rel in artifact_paths:
        target = _resolve_in_root(wt.path, rel)
        if not target.is_file():
            missing.append(rel)
            continue
        if target.stat().st_size > max_bytes:
            missing.append(f"{rel} (exceeds {max_bytes} bytes)")
            continue
        collected.append(target)
    return collected, missing


def run_verification(
    wt: Worktree,
    argv: list[str],
    *,
    executables: dict[str, str],
    timeout_seconds: float,
    max_output_bytes: int,
) -> VerifyResult:
    """Run the card's declared verification argv — trusted executables only,
    argv array only (never a shell string), bounded output."""
    if not argv:
        return VerifyResult(ok=False, exit_code=None, output="empty argv",
                            truncated=False)
    exe_name = Path(argv[0]).name
    resolved = executables.get(exe_name)
    if resolved is None:
        return VerifyResult(
            ok=False, exit_code=None,
            output=(
                f"verification executable {exe_name!r} is not in the policy "
                "executables allowlist"
            ),
            truncated=False,
        )
    cmd = [resolved, *[str(a) for a in argv[1:]]]
    try:
        proc = subprocess.run(
            cmd, cwd=str(wt.path), capture_output=True, text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        tail = ((exc.stdout or "") + (exc.stderr or ""))
        if isinstance(tail, bytes):
            tail = tail.decode("utf-8", "replace")
        return VerifyResult(
            ok=False, exit_code=None, output=tail[-max_output_bytes:],
            truncated=True, timed_out=True,
        )
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    truncated = len(output.encode("utf-8", "replace")) > max_output_bytes
    if truncated:
        output = output.encode("utf-8", "replace")[:max_output_bytes].decode(
            "utf-8", "replace"
        ) + "\n[output truncated]"
    return VerifyResult(
        ok=proc.returncode == 0, exit_code=proc.returncode, output=output,
        truncated=truncated,
    )


def remove_worktree(wt: Worktree, repo: Path) -> None:
    """Best-effort cleanup — partial worktrees are preserved on error paths,
    so this is only called on explicit success/cancel teardown."""
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt.path)],
        capture_output=True, timeout=60,
    )
