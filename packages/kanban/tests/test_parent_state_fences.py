"""Parent regressions: real two-connection interleavings, no live board."""
import os
from pathlib import Path
import subprocess
import textwrap

import pytest

from cli_provider_kanban import evidence
from cli_provider_kanban.kernel import hermes_python, hermes_repo
from conftest import requires_hermes

BRIDGE = Path(__file__).parents[1] / 'src/cli_provider_kanban/hermes_bridge.py'

SCRIPT = r'''
import importlib.util, sys
from pathlib import Path
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
spec = importlib.util.spec_from_file_location('bridge_under_test', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
b = m._Bridge(sys.argv[2]); other = connect(Path(sys.argv[2]))
tid = kb.create_task(b.conn, title='fixture', assignee='jev-native')
mode = sys.argv[3]
foreign_run = None
if mode == 'block':
    original = kb.block_task
    def interleaved(conn, task_id, **kwargs):
        global foreign_run
        kb.claim_task(other, task_id, claimer='new-owner', ttl_seconds=600)
        foreign_run = kb.get_task(other, task_id).current_run_id
        return original(conn, task_id, **kwargs)
    kb.block_task = interleaved
    try:
        result = b.op_block_owned({'task_id':tid, 'run_id':None,
                                   'kind':'needs_input', 'reason':'old request'})
    except Exception as exc:
        result = {'refused':type(exc).__name__}
    task = kb.get_task(other, tid)
    print(result, task.status, task.current_run_id, foreign_run)
    assert task.status == 'running' and task.current_run_id == foreign_run
elif mode == 'reopen':
    kb.claim_task(b.conn, tid, claimer='old', ttl_seconds=600)
    old = kb.get_task(b.conn, tid).current_run_id
    assert kb.request_review(b.conn, tid, expected_run_id=old)
    original = kb.reopen_review_task
    def interleaved(conn, task_id):
        global foreign_run
        assert original(other, task_id)
        kb.claim_task(other, task_id, claimer='new', ttl_seconds=600)
        foreign_run = kb.get_task(other, task_id).current_run_id
        assert kb.request_review(other, task_id, expected_run_id=foreign_run)
        return original(conn, task_id)
    kb.reopen_review_task = interleaved
    try:
        result = b.op_reopen_review_if({'task_id':tid, 'expected_run_id':old})
    except Exception as exc:
        result = {'refused':type(exc).__name__}
    task = kb.get_task(other, tid)
    print(result, task.status, foreign_run)
    assert task.status == 'review', 'stale check reopened newer review'
other.close(); b.conn.close()
'''


@requires_hermes
@pytest.mark.parametrize('mode', ['block', 'reopen'])
def test_kernel_fence_survives_second_connection(tmp_path, mode):
    script = tmp_path/'probe.py'; script.write_text(textwrap.dedent(SCRIPT))
    env = {'PATH':os.defpath, 'HOME':str(tmp_path),
           'HERMES_HOME':str(tmp_path/'home'), 'PYTHONPATH':hermes_repo(),
           'HERMES_KANBAN_BUSY_TIMEOUT_MS':'1000'}
    result = subprocess.run([hermes_python(), str(script), str(BRIDGE),
                             str(tmp_path/'board.db'), mode], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_private_write_checks_identity_before_truncating(tmp_path, monkeypatch):
    target = tmp_path/'out.diff'; target.write_text('old evidence')
    sentinel = tmp_path/'sentinel'; sentinel.write_text('KEEP THIS')
    real_open = os.open
    armed = True
    def raced_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal armed
        if armed and Path(path) == target:
            armed = False
            target.unlink()
            os.link(sentinel, target)
        return real_open(path, flags, mode, dir_fd=dir_fd)
    monkeypatch.setattr(evidence.os, 'open', raced_open)
    try:
        evidence._write_private(target, b'new evidence')
    except (evidence.EvidenceError, OSError):
        pass
    assert sentinel.read_text() == 'KEEP THIS'
