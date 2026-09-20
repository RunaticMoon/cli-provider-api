"""Parent regressions for EV3-1/2 and sibling admission bypasses.

All git writes target fresh tmp_path repositories, never source Git state.
"""
import os
import subprocess
from pathlib import Path

import pytest

from cli_provider_kanban import worktree as W

LOCK = ".cli-provider-runner.lock"


def git(root, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
               GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
    return subprocess.run(["git", "-C", str(root), *args], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def tree(tmp_path, name="src/module.py"):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("BASE = True\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed")
    base = git(repo, "rev-parse", "HEAD")
    wt = W.prepare_worktree(repo=repo, worktree_root=tmp_path / "trees",
                            workspace_id="ws", dispatch_id="d1", base_revision=base)
    return repo, wt


def admit(repo, wt):
    return W.validate_prepared_worktree(repo=repo, prepared=str(wt.path),
                                       base_revision=wt.base_revision, workspace_id="ws")


@pytest.mark.parametrize("name,flag", [
    (f"src/{LOCK}.py", None),
    (f"src/prefix{LOCK}", None),
    (f"src/line\n{LOCK}.py", None),
    ("src/module.py", "--assume-unchanged"),
    ("src/module.py", "--skip-worktree"),
])
def test_admission_cannot_hide_modified_tracked_content(tmp_path, name, flag):
    repo, wt = tree(tmp_path, name)
    if flag:
        git(wt.path, "update-index", flag, "--", name)
    (wt.path / name).write_text("BASE = False\n")
    with pytest.raises(W.WorktreeError):
        admit(repo, wt)
    assert (wt.path / name).read_text() == "BASE = False\n"


@pytest.mark.parametrize("entrypoint", [W.changed_files, lambda wt: W.capture_diff(wt, 65536)])
@pytest.mark.parametrize("shape", ["directory", "symlink", "hardlink", "fifo"])
def test_lock_name_exemption_requires_real_single_link_file(tmp_path, entrypoint, shape):
    _repo, wt = tree(tmp_path)
    lock = wt.path / LOCK
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("EXTERNAL-SENTINEL\n")
    if shape == "directory":
        lock.mkdir()
        (lock / "payload.txt").write_text("HIDDEN-PAYLOAD\n")
    elif shape == "symlink":
        lock.symlink_to(sentinel)
    elif shape == "hardlink":
        os.link(sentinel, lock)
    else:
        os.mkfifo(lock)
    with pytest.raises(W.WorktreeError):
        entrypoint(wt)
    assert sentinel.read_text() == "EXTERNAL-SENTINEL\n"


def test_lock_directory_patch_never_silently_omits_content(tmp_path):
    _repo, wt = tree(tmp_path)
    lock = wt.path / LOCK
    lock.mkdir()
    (lock / "payload.txt").write_text("HIDDEN-PAYLOAD\n")
    (wt.path / "normal.txt").write_text("ordinary output\n")
    try:
        changed = W.changed_files(wt)
        diff = W.capture_diff(wt, 65536)
    except W.WorktreeError:
        return  # Refusal is safe; a complete-looking partial patch is not.
    assert f"{LOCK}/payload.txt" in changed
    assert "ordinary output" in diff
    assert "HIDDEN-PAYLOAD" in diff


def test_regular_lock_positive_and_nested_lookalike_visible(tmp_path):
    repo, wt = tree(tmp_path)
    (wt.path / LOCK).write_text("fixture lock\n")
    admit(repo, wt)
    (wt.path / "sub").mkdir()
    (wt.path / "sub" / LOCK).write_text("NESTED-MARKER\n")
    assert f"sub/{LOCK}" in W.changed_files(wt)
    diff = W.capture_diff(wt, 65536)
    assert "NESTED-MARKER" in diff
    assert "fixture lock" not in diff


def test_staged_rename_onto_lock_substring_still_rejected(tmp_path):
    """A staged rename landing on a lock-substring path must still fail
    admission — the exact-name exemption never widens to staged state."""
    repo, wt = tree(tmp_path)
    git(wt.path, "mv", "src/module.py", f"src/{LOCK}.py")
    with pytest.raises(W.WorktreeError):
        admit(repo, wt)


def test_index_only_staged_change_still_rejected(tmp_path):
    """A shared-index staged change with untouched disk bytes is still a
    dirty tree — the private index cannot see it, so the shared status
    check remains part of admission."""
    repo, wt = tree(tmp_path)
    git(wt.path, "rm", "-q", "--cached", "src/module.py")
    assert (wt.path / "src/module.py").read_text() == "BASE = True\n"
    with pytest.raises(W.WorktreeError):
        admit(repo, wt)


def test_shared_index_bytes_unchanged_by_evidence(tmp_path):
    """Evidence/admission never mutate the shared index — flags, staged
    entries and mtime all survive untouched."""
    repo, wt = tree(tmp_path)
    gitdir = Path(git(wt.path, "rev-parse", "--git-dir"))
    if not gitdir.is_absolute():
        gitdir = (wt.path / gitdir).resolve()
    index = gitdir / "index"
    before = index.read_bytes()
    (wt.path / "new.txt").write_text("n\n")
    with pytest.raises(W.WorktreeError):
        admit(repo, wt)
    assert "new.txt" in W.changed_files(wt)
    W.capture_diff(wt, 65536)
    assert index.read_bytes() == before


def test_clean_tree_with_regular_lock_admits_clean_evidence(tmp_path):
    """The legitimate case still works: a clean tree carrying only the
    ordinary Runner lock file admits and produces empty evidence."""
    repo, wt = tree(tmp_path)
    (wt.path / LOCK).write_text("pid=1\n")
    admitted = admit(repo, wt)
    assert admitted.path == wt.path
    assert W.changed_files(wt) == []
    assert "pid=1" not in W.capture_diff(wt, 65536)


def test_lock_name_tracked_in_base_is_ordinary_evidence(tmp_path):
    """A base-tracked file named exactly like the lock is application data,
    not infra bookkeeping — it is never exempted by name alone."""
    repo, wt = tree(tmp_path, name=LOCK)
    (wt.path / LOCK).write_text("TAMPERED\n")
    with pytest.raises(W.WorktreeError):
        admit(repo, wt)
    assert LOCK in W.changed_files(wt)
    assert "TAMPERED" in W.capture_diff(wt, 65536)
