"""Evidence-path repair proofs (S2/S3 + buffer bounds) — real git repos.

The run owns the tree it is measured on: evidence must fail closed when
git breaks or the root moves, must see ignored/excluded/untracked/dotfile/
tricky-name/symlink paths, and must bound the diff while reading it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cli_provider_kanban import worktree as wmod
from cli_provider_kanban.worktree import (
    WorktreeError,
    capture_diff,
    changed_files,
    check_scope,
    prepare_worktree,
    remove_worktree,
)

from conftest import git_repo  # noqa: F401  (fixture)

ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
    GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
)


def _git(path: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *argv], env=ENV, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _wt(git_repo, tmp_path, dispatch_id="d_ev"):
    repo, base = git_repo
    return repo, prepare_worktree(
        repo=repo, worktree_root=tmp_path / "wt",
        workspace_id="ws", dispatch_id=dispatch_id, base_revision=base,
    )


class TestBoundTreeRevalidation:
    """S2 — a broken/moved/retargeted tree is a HARD failure, never an
    empty evidence set."""

    def test_git_link_removed_fails_closed(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        (wt.path / "etc_shadowish.txt").write_text("out-of-scope\n")
        (wt.path / ".git").unlink()
        with pytest.raises(WorktreeError):
            changed_files(wt)
        with pytest.raises(WorktreeError):
            capture_diff(wt, 262144)
        # The out-of-scope write is still really there — the failure is in
        # the evidence path, not a fabricated clean tree.
        assert (wt.path / "etc_shadowish.txt").is_file()

    def test_git_link_retargeted_fails_closed(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        foreign = tmp_path / "foreign"
        subprocess.run(["git", "init", "-q", str(foreign)], env=ENV,
                       check=True, capture_output=True)
        (wt.path / ".git").write_text(f"gitdir: {foreign / '.git'}\n")
        with pytest.raises(WorktreeError):
            changed_files(wt)

    def test_head_must_descend_from_base(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        _git(wt.path, "checkout", "-q", "--orphan", "foreign-line")
        _git(wt.path, "rm", "-rfq", ".")
        (wt.path / "o.txt").write_text("o")
        _git(wt.path, "add", "-A")
        _git(wt.path, "commit", "-qm", "o")
        with pytest.raises(WorktreeError):
            changed_files(wt)
        with pytest.raises(WorktreeError):
            capture_diff(wt, 262144)

    def test_committed_work_is_legitimate(self, git_repo, tmp_path):
        """The worker MAY commit — HEAD != base is not itself a failure."""
        repo, wt = _wt(git_repo, tmp_path)
        (wt.path / "committed.txt").write_text("work\n")
        _git(wt.path, "add", "-A")
        _git(wt.path, "commit", "-qm", "card work")
        assert changed_files(wt) == ["committed.txt"]
        assert "committed.txt" in capture_diff(wt, 262144)
        remove_worktree(wt, repo)

    def test_root_relocation_fails_closed(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        moved = wt.path.with_name("wt-moved")
        os.rename(wt.path, moved)
        os.symlink(moved, wt.path)
        try:
            with pytest.raises(WorktreeError):
                changed_files(wt)
        finally:
            wt.path.unlink()
            os.rename(moved, wt.path)
        remove_worktree(wt, repo)

    def test_missing_worktree_fails_closed(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path)
        remove_worktree(wt, repo)
        with pytest.raises(WorktreeError):
            changed_files(wt)


class TestCompleteInventory:
    """S3 — ignore rules, exclude files, index flags and tricky names
    cannot hide output from the scope gate."""

    def test_ignored_files_visible_and_scoped(self, git_repo, tmp_path):
        repo, base = git_repo
        (repo / ".gitignore").write_text("dist/\n*.log\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "ignore rules")
        base = _git(repo, "rev-parse", "HEAD")
        wt = prepare_worktree(
            repo=repo, worktree_root=tmp_path / "wt", workspace_id="ws",
            dispatch_id="d_ign", base_revision=base,
        )
        (wt.path / "src").mkdir()
        (wt.path / "src" / "ok.txt").write_text("in-scope\n")
        (wt.path / "dist").mkdir()
        (wt.path / "dist" / "payload.sh").write_text("#!/bin/sh\necho x\n")
        (wt.path / "harvest.log").write_text("API_KEY=abc\n")
        changed = changed_files(wt)
        assert {"src/ok.txt", "dist/payload.sh", "harvest.log"} <= set(changed)
        assert check_scope(changed, ["src/"]) == [
            "dist/payload.sh", "harvest.log",
        ]
        diff = capture_diff(wt, 262144)
        assert "payload.sh" in diff and "harvest.log" in diff
        remove_worktree(wt, repo)

    def test_info_exclude_cannot_hide(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path, "d_exc")
        common = _git(wt.path, "rev-parse", "--git-common-dir")
        common = Path(common)
        if not common.is_absolute():
            common = wt.path / common
        (common / "info").mkdir(parents=True, exist_ok=True)
        (common / "info" / "exclude").write_text("sneaky.txt\n")
        (wt.path / "sneaky.txt").write_text("out of scope\n")
        assert "sneaky.txt" in changed_files(wt)
        assert check_scope(changed_files(wt), ["src/"]) == ["sneaky.txt"]
        remove_worktree(wt, repo)

    def test_dotfiles_and_tricky_names(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path, "d_names")
        names = [
            ".hidden",
            ".config/toolrc",
            "new\nline.txt",
            " space.txt",
            "uni-ü.txt",
        ]
        (wt.path / ".config").mkdir()
        for name in names:
            (wt.path / name).write_text("x\n")
        changed = changed_files(wt)
        for name in names:
            assert name in changed, name
        remove_worktree(wt, repo)

    def test_assume_unchanged_cannot_hide(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path, "d_au")
        (wt.path / "seed.txt").write_text("secretly modified\n")
        _git(wt.path, "update-index", "--assume-unchanged", "seed.txt")
        assert "seed.txt" in changed_files(wt)
        remove_worktree(wt, repo)

    def test_skip_worktree_cannot_hide(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path, "d_sw")
        (wt.path / "seed.txt").write_text("secretly modified\n")
        _git(wt.path, "update-index", "--skip-worktree", "seed.txt")
        assert "seed.txt" in changed_files(wt)
        remove_worktree(wt, repo)

    def test_deletion_rename_commit_and_dirty(self, git_repo, tmp_path):
        repo, base = git_repo
        (repo / "keep.txt").write_text("keep\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "second file")
        base = _git(repo, "rev-parse", "HEAD")
        wt = prepare_worktree(
            repo=repo, worktree_root=tmp_path / "wt", workspace_id="ws",
            dispatch_id="d_mix", base_revision=base,
        )
        (wt.path / "seed.txt").unlink()                # tracked deletion
        _git(wt.path, "mv", "keep.txt", "moved.txt")   # staged rename
        (wt.path / "committed.txt").write_text("c\n")  # committed work
        _git(wt.path, "add", "-A")
        _git(wt.path, "commit", "-qm", "card work")
        (wt.path / "committed.txt").write_text("dirty\n")  # + dirty edit
        changed = changed_files(wt)
        assert {"seed.txt", "keep.txt", "moved.txt", "committed.txt"} <= set(
            changed
        )
        remove_worktree(wt, repo)

    def test_symlink_recorded_never_followed(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path, "d_sym")
        (wt.path / "esc").symlink_to("/etc/hostname")
        (wt.path / "dirlink").symlink_to("/tmp")
        changed = changed_files(wt)
        assert "esc" in changed
        assert "dirlink" in changed            # itself, not its contents
        assert not any(p.startswith("dirlink/") for p in changed)
        diff = capture_diff(wt, 262144)
        assert "esc" in diff
        remove_worktree(wt, repo)

    def test_runner_lock_excluded_from_both_views(
        self, git_repo, tmp_path
    ):
        repo, wt = _wt(git_repo, tmp_path, "d_lock")
        (wt.path / ".cli-provider-runner.lock").write_text("pid=1\n")
        (wt.path / "real.txt").write_text("output\n")
        changed = changed_files(wt)
        assert changed == ["real.txt"]
        diff = capture_diff(wt, 262144)
        assert "real.txt" in diff
        assert ".cli-provider-runner.lock" not in diff
        remove_worktree(wt, repo)

    def test_no_shared_index_residue(self, git_repo, tmp_path):
        """Evidence must not leave intent-to-add entries or mutate the
        shared index/exclude state to take its measurement."""
        repo, wt = _wt(git_repo, tmp_path, "d_res")
        (wt.path / "untracked.txt").write_text("u\n")
        before = _git(wt.path, "status", "--porcelain", "-uall")
        before_ls = _git(wt.path, "ls-files")
        changed_files(wt)
        capture_diff(wt, 262144)
        assert _git(wt.path, "status", "--porcelain", "-uall") == before
        assert _git(wt.path, "ls-files") == before_ls
        assert "untracked.txt" not in before_ls.split()
        remove_worktree(wt, repo)


class TestDangerousConfigKeys:
    """Model-writable repo config can hide executable config behind ANY
    valid subsection syntax — every enumerated dangerous key is reset via
    safe argv, or refuses evidence before content git runs. No marker may
    ever execute."""

    def test_filter_subsection_odd_names_cannot_execute(
        self, git_repo, tmp_path
    ):
        repo, wt = _wt(git_repo, tmp_path, "d_cfg")
        for i, name in enumerate(
            ["bad/name", "bad name", 'bad"name', "bad;name", "bad$(id)x"]
        ):
            marker = tmp_path / f"marker-{i}"
            _git(wt.path, "config", f"filter.{name}.clean",
                 f"touch {marker}; cat")
            (wt.path / ".gitattributes").write_text(
                f"*.payload filter={name}\n"
            )
            (wt.path / f"f{i}.payload").write_text("sample\n")
            changed_files(wt)
            capture_diff(wt, 8192)
            assert not marker.exists(), name
        # Positive: ordinary evidence still works on this tree.
        assert "f0.payload" in changed_files(wt)
        remove_worktree(wt, repo)

    def test_included_config_filter_cannot_execute(
        self, git_repo, tmp_path
    ):
        repo, wt = _wt(git_repo, tmp_path, "d_inc")
        marker = tmp_path / "inc-executed"
        extra = tmp_path / "extra.cfg"
        extra.write_text(f'[filter "viainc"]\n\tclean = touch {marker}\n')
        _git(wt.path, "config", "include.path", str(extra))
        (wt.path / ".gitattributes").write_text("*.payload filter=viainc\n")
        (wt.path / "x.payload").write_text("p\n")
        assert "x.payload" in changed_files(wt)
        capture_diff(wt, 8192)
        assert not marker.exists()
        remove_worktree(wt, repo)

    def test_process_filter_cannot_execute(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path, "d_prc")
        marker = tmp_path / "proc-executed"
        _git(wt.path, "config", "filter.evil.process",
             f"touch {marker}; cat")
        _git(wt.path, "config", "filter.evil.clean",
             f"touch {marker}; cat")
        (wt.path / ".gitattributes").write_text("*.payload filter=evil\n")
        (wt.path / "x.payload").write_text("p\n")
        changed_files(wt)
        capture_diff(wt, 8192)
        assert not marker.exists()
        remove_worktree(wt, repo)

    def test_unexpressible_dangerous_key_refuses_before_content(
        self, git_repo, tmp_path
    ):
        """A subsection containing '=' cannot be expressed as a `-c`
        override (it would silently reset a DIFFERENT key) — evidence
        must refuse with WorktreeError instead of running unguarded."""
        repo, wt = _wt(git_repo, tmp_path, "d_feq")
        marker = tmp_path / "eq-executed"
        common = Path(_git(wt.path, "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = wt.path / common
        with open(common / "config", "a") as fh:
            fh.write(f'[filter "bad=name"]\n\tclean = touch {marker}\n')
        (wt.path / ".gitattributes").write_text("*.payload filter=bad=name\n")
        (wt.path / "x.payload").write_text("p\n")
        with pytest.raises(WorktreeError):
            changed_files(wt)
        with pytest.raises(WorktreeError):
            capture_diff(wt, 8192)
        assert not marker.exists()
        remove_worktree(wt, repo)


class TestRunCaptureBounds:
    def test_exited_child_holding_inherited_pipe_bounded(self):
        """A dead leader whose grandchild holds the stdout write end must
        not turn a not-ready select into a blocking os.read — that would
        hang past the deadline on an open-but-silent pipe."""
        argv = [
            sys.executable, "-c",
            "import subprocess;subprocess.Popen(['sleep','30'])",
        ]
        start = time.monotonic()
        with pytest.raises(WorktreeError):
            wmod._run_capture(argv, env={}, cap=4096, timeout=1.5)
        # The deadline fires at ~1.5s; the bounded stderr-drain join adds
        # at most 5s. A blocking read would have hung ~30s on the open
        # pipe and returned normally instead of raising.
        assert time.monotonic() - start < 10


class TestBoundedDiff:
    def test_streaming_cap_labels_truncation(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path, "d_big")
        (wt.path / "big.bin").write_bytes(os.urandom(2 * 1024 * 1024))
        diff = capture_diff(wt, max_bytes=65536)
        assert "[diff truncated]" in diff
        # The cap is enforced while reading — the returned text is bounded.
        assert len(diff.encode("utf-8", "replace")) <= 65536 + 64
        remove_worktree(wt, repo)

    def test_invalid_pinned_base_fails_closed(self, git_repo, tmp_path):
        repo, wt = _wt(git_repo, tmp_path, "d_badb")
        object.__setattr__(wt, "base_revision", "0" * 40)
        with pytest.raises(WorktreeError):
            changed_files(wt)
        with pytest.raises(WorktreeError):
            capture_diff(wt, 262144)
        remove_worktree(wt, repo)
