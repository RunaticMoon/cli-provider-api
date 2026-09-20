"""Parent safety regressions on the completed evidence repair."""
import os
import subprocess
from pathlib import Path

import pytest

from cli_provider_kanban import _verify_supervisor as vs
from cli_provider_kanban.worktree import (
    WorktreeError, capture_diff, changed_files, prepare_worktree,
)
from conftest import git_repo  # noqa: F401


def _tree(git_repo, tmp_path):
    repo, base = git_repo
    return repo, prepare_worktree(repo=repo, worktree_root=tmp_path/'trees',
                                 workspace_id='ws', dispatch_id='parent',
                                 base_revision=base)


def test_verifier_named_directory_cannot_hide_worker_output(git_repo, tmp_path):
    _, wt = _tree(git_repo, tmp_path)
    (wt.path/'.jev-verify').mkdir()
    (wt.path/'.jev-verify/payload.txt').write_text('out of scope\n')
    assert '.jev-verify/payload.txt' in changed_files(wt)
    assert 'payload.txt' in capture_diff(wt, 8192)


def test_proc_enumeration_error_never_certifies_no_survivors(monkeypatch):
    def unavailable(path):
        raise PermissionError('synthetic proc unavailable')
    monkeypatch.setattr(vs.os, 'listdir', unavailable)
    with pytest.raises(Exception):
        vs._kill_tree(os.getpid(), 0.1, _kill=lambda *_: None)


def test_unsafe_git_filter_subsection_cannot_execute(git_repo, tmp_path):
    repo, wt = _tree(git_repo, tmp_path)
    marker = tmp_path/'filter-executed'
    def git(*args):
        return subprocess.run(['git', '-C', str(wt.path), *args],
                              check=True, capture_output=True)
    git('config', 'filter.bad/name.clean', f'touch {marker}; cat')
    (wt.path/'.gitattributes').write_text('*.payload filter=bad/name\n')
    (wt.path/'test.payload').write_text('sample\n')
    try:
        changed_files(wt)
        capture_diff(wt, 8192)
    except WorktreeError:
        pass  # A typed pre-read rejection is acceptable; execution is not.
    assert not marker.exists(), 'evidence inspection ran repository filter'
