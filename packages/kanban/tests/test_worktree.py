"""Trusted-worktree tests — real git repos/worktrees only."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from cli_provider_kanban.worktree import (
    WorktreeError,
    assert_is_ancestor,
    branch_name,
    capture_diff,
    check_dependencies,
    collect_artifacts,
    prepare_worktree,
    remove_worktree,
    resolve_repo,
    run_verification,
    validate_commit,
)

from conftest import git_repo  # noqa: F401  (fixture)
from conftest import make_git_repo


def _commit(repo: Path, name: str, content: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    (repo / name).write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", name], cwd=repo, env=env,
                   check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, env=env, check=True,
        capture_output=True, text=True).stdout.strip()


class TestRepoValidation:
    def test_resolve_repo_accepts_git(self, git_repo):
        repo, _ = git_repo
        assert resolve_repo(str(repo)) == repo.resolve()

    def test_resolve_repo_rejects_nonrepo(self, tmp_path):
        with pytest.raises(WorktreeError):
            resolve_repo(str(tmp_path / "nope"))

    def test_resolve_repo_rejects_nongit(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(WorktreeError):
            resolve_repo(str(plain))


class TestCommitValidation:
    def test_full_sha_ok(self, git_repo):
        _, rev = git_repo
        validate_commit(rev)

    @pytest.mark.parametrize("bad", [
        "abc123",                       # short
        "z" * 40,                       # non-hex
        "../main",                      # traversal
        "HEAD~1",                       # symbolic
        "main;rm -rf /",                # injection
        "",
    ])
    def test_rejects_non_full_sha(self, bad):
        with pytest.raises(WorktreeError):
            validate_commit(bad)


class TestAncestry:
    def test_direct_ancestor(self, git_repo):
        repo, base = git_repo
        child = _commit(repo, "b.txt", "x")
        assert_is_ancestor(repo, base, child)

    def test_nonancestor_rejected(self, git_repo):
        repo, base = git_repo
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "checkout", "-q", "--orphan", "orph"],
                       cwd=repo, env=env, check=True, capture_output=True)
        subprocess.run(["git", "rm", "-rfq", "."], cwd=repo, env=env,
                       check=True, capture_output=True)
        (repo / "o.txt").write_text("o")
        subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-qm", "o"], cwd=repo, env=env,
                       check=True, capture_output=True)
        other = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, env=env, check=True,
            capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "checkout", "-q", "master"], cwd=repo, env=env,
                       check=True, capture_output=True)
        with pytest.raises(WorktreeError, match="not an ancestor"):
            assert_is_ancestor(repo, other, base)

    def test_check_dependencies_missing_revision(self, git_repo):
        repo, base = git_repo
        checks = check_dependencies(repo, base, [("t_dep", None)])
        assert checks[0].ok is False
        assert "integrated_revision" in checks[0].reason

    def test_check_dependencies_ancestor_ok(self, git_repo):
        repo, base = git_repo
        checks = check_dependencies(repo, base, [("t_dep", base)])
        assert checks[0].ok is True


class TestPrepareWorktree:
    def test_creates_at_base_with_branch(self, git_repo, tmp_path):
        repo, base = git_repo
        newer = _commit(repo, "later.txt", "after-base")
        wt = prepare_worktree(
            repo=repo, worktree_root=tmp_path / "wt",
            workspace_id="ws-main", dispatch_id="d_abc123",
            base_revision=base,
        )
        assert wt.path.is_dir()
        assert wt.branch == "jev/d_abc123"
        assert (wt.path / "seed.txt").is_file()
        assert not (wt.path / "later.txt").exists()  # pinned at base
        remove_worktree(wt, repo)

    def test_deterministic_branch_name(self):
        # Path separators and dots can never enter the ref name.
        assert branch_name("d_x/../y") == "jev/d_x----y"
        assert branch_name("d_abc") == "jev/d_abc"

    def test_rejects_bad_base(self, git_repo, tmp_path):
        repo, _ = git_repo
        with pytest.raises(WorktreeError):
            prepare_worktree(
                repo=repo, worktree_root=tmp_path / "wt",
                workspace_id="ws", dispatch_id="d_1", base_revision="bad!",
            )

    def test_rejects_unknown_base(self, git_repo, tmp_path):
        repo, _ = git_repo
        with pytest.raises(WorktreeError):
            prepare_worktree(
                repo=repo, worktree_root=tmp_path / "wt",
                workspace_id="ws", dispatch_id="d_1",
                base_revision="0" * 40,
            )

    def test_path_escape_rejected(self, git_repo, tmp_path):
        repo, base = git_repo
        with pytest.raises(WorktreeError):
            prepare_worktree(
                repo=repo, worktree_root=tmp_path / "wt",
                workspace_id="ws", dispatch_id="..%2f..%2fescape".replace(
                    "%2f", "/"),
                base_revision=base,
            )


class TestArtifacts:
    def test_collect_and_missing(self, git_repo, tmp_path):
        repo, base = git_repo
        wt = prepare_worktree(
            repo=repo, worktree_root=tmp_path / "wt",
            workspace_id="ws", dispatch_id="d_art", base_revision=base,
        )
        (wt.path / "out.txt").write_text("artifact")
        got, missing = collect_artifacts(
            wt, ["out.txt", "missing.txt"], max_bytes=4096)
        assert [p.name for p in got] == ["out.txt"]
        assert missing == ["missing.txt"]
        remove_worktree(wt, repo)

    def test_traversal_rejected(self, git_repo, tmp_path):
        repo, base = git_repo
        wt = prepare_worktree(
            repo=repo, worktree_root=tmp_path / "wt",
            workspace_id="ws", dispatch_id="d_tr", base_revision=base,
        )
        with pytest.raises(WorktreeError):
            collect_artifacts(wt, ["../../etc/passwd"], max_bytes=4096)
        with pytest.raises(WorktreeError):
            collect_artifacts(wt, ["/etc/passwd"], max_bytes=4096)
        remove_worktree(wt, repo)

    def test_symlink_escape_rejected(self, git_repo, tmp_path):
        repo, base = git_repo
        wt = prepare_worktree(
            repo=repo, worktree_root=tmp_path / "wt",
            workspace_id="ws", dispatch_id="d_sl", base_revision=base,
        )
        # Resolves to /etc/hostname — outside the worktree root.
        (wt.path / "link").symlink_to("/etc/hostname")
        with pytest.raises(WorktreeError):
            collect_artifacts(wt, ["link"], max_bytes=4096)
        remove_worktree(wt, repo)


class TestVerification:
    def _wt(self, git_repo, tmp_path):
        repo, base = git_repo
        return repo, prepare_worktree(
            repo=repo, worktree_root=tmp_path / "wt",
            workspace_id="ws", dispatch_id="d_ver", base_revision=base,
        )

    def test_allowlisted_executable(self, git_repo, tmp_path):
        repo, wt = self._wt(git_repo, tmp_path)
        res = run_verification(
            wt, ["true"], executables={"true": "/usr/bin/true"},
            timeout_seconds=10, max_output_bytes=1024,
        )
        assert res.ok and res.exit_code == 0
        remove_worktree(wt, repo)

    def test_failing_executable(self, git_repo, tmp_path):
        repo, wt = self._wt(git_repo, tmp_path)
        res = run_verification(
            wt, ["false"], executables={"false": "/usr/bin/false"},
            timeout_seconds=10, max_output_bytes=1024,
        )
        assert not res.ok and res.exit_code == 1
        remove_worktree(wt, repo)

    def test_unallowlisted_executable_refused(self, git_repo, tmp_path):
        repo, wt = self._wt(git_repo, tmp_path)
        res = run_verification(
            wt, ["rm", "-rf", "."], executables={},
            timeout_seconds=10, max_output_bytes=1024,
        )
        assert not res.ok and res.exit_code is None
        assert "allowlist" in res.output
        remove_worktree(wt, repo)

    def test_argv_only_never_shell(self, git_repo, tmp_path):
        repo, wt = self._wt(git_repo, tmp_path)
        # A shell metachar arg must be passed literally — echo prints it.
        res = run_verification(
            wt, ["echo", "a;b"], executables={"echo": "/usr/bin/echo"},
            timeout_seconds=10, max_output_bytes=1024,
        )
        assert res.ok and "a;b" in res.output
        remove_worktree(wt, repo)


class TestDiffCapture:
    def test_bounded_diff(self, git_repo, tmp_path):
        repo, base = git_repo
        wt = prepare_worktree(
            repo=repo, worktree_root=tmp_path / "wt",
            workspace_id="ws", dispatch_id="d_diff", base_revision=base,
        )
        (wt.path / "big.txt").write_text("x" * 10000)
        diff = capture_diff(wt, max_bytes=512)
        assert len(diff) <= 512 + 64
        assert "truncated" in diff
        remove_worktree(wt, repo)
