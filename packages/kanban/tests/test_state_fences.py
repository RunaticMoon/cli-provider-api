"""State-fence regressions for the guarded bridge ops — the CLASS the parent
probes sampled, not just the two probes themselves.

The kernel-fence tests are deterministic two-connection interleavings run
in a subprocess against the real installed Hermes kernel on a temp board:
the second connection's mutation is injected INSIDE the wrapped kernel
mutator (after the bridge's pre-read, before its write transaction), so no
sleeps and no live board are involved. The evidence tests inject the swap
at ``os.open`` — the last possible instant before the FD exists.
"""
import os
from pathlib import Path
import subprocess
import textwrap

import pytest

from cli_provider_kanban import evidence
from cli_provider_kanban.kernel import hermes_python, hermes_repo
from conftest import (  # noqa: F401
    dispatch_policy,
    requires_hermes,
    stub_wrapper,
    write_policy,
    write_runner_config,
)
from test_state_repair import _card, _policy_path, _tick, env  # noqa: F401

pytestmark = requires_hermes

BRIDGE = Path(__file__).parents[1] / "src/cli_provider_kanban/hermes_bridge.py"

# Every mode creates the task, then wraps ONE kernel mutator so a SECOND
# independent connection mutates the card between the bridge op's pre-read
# and the wrapped mutator's own BEGIN IMMEDIATE — the exact interleaving the
# lock-held guards exist for.
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

def last_block_reason():
    row = other.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('blocked','block_loop_detected','dependency_wait') "
        "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    return row[0] if row else None

if mode == 'unblock_reclaim':
    # Our run is blocked; a second connection unblocks and claims between
    # our read and the fenced unblock — the card is no longer blocked.
    kb.claim_task(b.conn, tid, claimer='old', ttl_seconds=600)
    ours = kb.get_task(b.conn, tid).current_run_id
    kb.block_task(b.conn, tid, reason='dispatch park [dispatch d9]')
    original = kb.unblock_task
    def interleaved(conn, task_id):
        global foreign_run
        assert original(other, task_id)
        kb.claim_task(other, task_id, claimer='new-owner', ttl_seconds=600)
        foreign_run = kb.get_task(other, task_id).current_run_id
        return original(conn, task_id)
    kb.unblock_task = interleaved
    result = b.op_unblock_owned({
        'task_id': tid, 'run_id': ours, 'latest_run_id': ours,
        'reason_suffix': '[dispatch d9]'})
    task = kb.get_task(other, tid)
    print(result, task.status, task.current_run_id, foreign_run)
    assert result.get('unblocked') is not True
    assert task.status == 'running' and task.current_run_id == foreign_run

elif mode == 'unblock_reblock':
    # Harder case: the card is still held when our mutator runs — but the
    # hold on it is a different actor's, not the one we attributed (the
    # kernel may escalate the repeat block to triage; either way our
    # fenced unblock must not clear it).
    kb.claim_task(b.conn, tid, claimer='old', ttl_seconds=600)
    ours = kb.get_task(b.conn, tid).current_run_id
    kb.block_task(b.conn, tid, reason='dispatch park [dispatch d9]')
    original = kb.unblock_task
    def interleaved(conn, task_id):
        assert original(other, task_id)
        kb.block_task(other, task_id, reason='operator re-fence')
        return original(conn, task_id)
    kb.unblock_task = interleaved
    result = b.op_unblock_owned({
        'task_id': tid, 'run_id': ours, 'latest_run_id': ours,
        'reason_suffix': '[dispatch d9]'})
    task = kb.get_task(other, tid)
    print(result, task.status, last_block_reason())
    assert result.get('unblocked') is not True
    assert task.status in ('blocked', 'triage')
    assert 'operator re-fence' in (last_block_reason() or '')

elif mode == 'claim_newer':
    # Card is ready, our run ended; between our read and the fenced claim a
    # second connection claims a run and leaves the card ready again — the
    # plain CAS would succeed, the run fence must refuse.
    kb.claim_task(b.conn, tid, claimer='old', ttl_seconds=600)
    ours = kb.get_task(b.conn, tid).current_run_id
    kb.block_task(b.conn, tid, reason='park [dispatch d9]')
    kb.unblock_task(b.conn, tid)
    original = kb.claim_task
    def interleaved(conn, task_id, **kwargs):
        global foreign_run
        original(other, task_id, claimer='new', ttl_seconds=600)
        foreign_run = kb.get_task(other, task_id).current_run_id
        kb.block_task(other, task_id, reason='foreign park')
        kb.unblock_task(other, task_id)
        return original(conn, task_id, **kwargs)
    kb.claim_task = interleaved
    result = b.op_claim({'task_id': tid, 'claimer': 'resolve',
                         'expected_run_id': ours})
    task = kb.get_task(other, tid)
    nruns = other.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)
    ).fetchone()[0]
    print(result, task.status, task.current_run_id, foreign_run, nruns)
    assert result.get('claimed') is False
    # We never claimed and never created a run — the card carries whatever
    # state the foreign actor left (the repeated blocks may escalate to
    # triage), but never a running claim under our stale expectation.
    assert task.status != 'running' and task.current_run_id is None
    assert nruns == 2  # our ended run + the foreign one — we created none

elif mode == 'block_owned_positive':
    # Positive control: the true owner blocks its own live run.
    kb.claim_task(b.conn, tid, claimer='old', ttl_seconds=600)
    ours = kb.get_task(b.conn, tid).current_run_id
    result = b.op_block_owned({'task_id': tid, 'run_id': ours,
                               'kind': 'needs_input', 'reason': 'hold'})
    task = kb.get_task(other, tid)
    print(result, task.status)
    assert result.get('blocked') is True and task.status == 'blocked'

elif mode == 'unblock_owned_positive':
    # Positive control: the owner unblocks the block it filed.
    kb.claim_task(b.conn, tid, claimer='old', ttl_seconds=600)
    ours = kb.get_task(b.conn, tid).current_run_id
    kb.block_task(b.conn, tid, reason='dispatch park [dispatch d9]')
    result = b.op_unblock_owned({
        'task_id': tid, 'run_id': ours, 'latest_run_id': ours,
        'reason_suffix': '[dispatch d9]'})
    task = kb.get_task(other, tid)
    print(result, task.status)
    assert result.get('unblocked') is True and task.status == 'ready'

elif mode == 'reopen_positive':
    # Positive control: the owner of the review handoff reopens it.
    kb.claim_task(b.conn, tid, claimer='old', ttl_seconds=600)
    ours = kb.get_task(b.conn, tid).current_run_id
    assert kb.request_review(b.conn, tid, expected_run_id=ours)
    result = b.op_reopen_review_if({'task_id': tid, 'expected_run_id': ours})
    task = kb.get_task(other, tid)
    print(result, task.status)
    assert result.get('reopened') is True and task.status != 'review'

elif mode == 'claim_positive':
    # Positive control: fenced claim succeeds when no newer run exists.
    kb.claim_task(b.conn, tid, claimer='old', ttl_seconds=600)
    ours = kb.get_task(b.conn, tid).current_run_id
    kb.block_task(b.conn, tid, reason='park [dispatch d9]')
    kb.unblock_task(b.conn, tid)
    result = b.op_claim({'task_id': tid, 'claimer': 'resolve',
                         'expected_run_id': ours})
    task = kb.get_task(other, tid)
    print(result, task.status, task.current_run_id)
    assert result.get('claimed') is True
    assert task.status == 'running' and task.current_run_id is not None

other.close(); b.conn.close()
'''

MODES = [
    "unblock_reclaim",
    "unblock_reblock",
    "claim_newer",
    "block_owned_positive",
    "unblock_owned_positive",
    "reopen_positive",
    "claim_positive",
]


@requires_hermes
@pytest.mark.parametrize("mode", MODES)
def test_fenced_ops_under_second_connection(tmp_path, mode):
    script = tmp_path / "probe.py"
    script.write_text(textwrap.dedent(SCRIPT))
    env = {"PATH": os.defpath, "HOME": str(tmp_path),
           "HERMES_HOME": str(tmp_path / "home"),
           "PYTHONPATH": hermes_repo(),
           "HERMES_KANBAN_BUSY_TIMEOUT_MS": "1000"}
    result = subprocess.run(
        [hermes_python(), str(script), str(BRIDGE),
         str(tmp_path / "board.db"), mode],
        env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


# -- evidence._write_private: verify-then-truncate, never truncate-first ------

def test_hardlink_swap_never_truncates_sentinel(tmp_path, monkeypatch):
    """The leaf is swapped for a hardlink to a sentinel at the instant of
    open — no O_TRUNC, so the shared inode is never erased, and fstat then
    refuses the multiply-linked target."""
    target = tmp_path / "out.diff"
    target.write_text("old evidence")
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("KEEP THIS")
    real_open = os.open
    armed = True

    def raced_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal armed
        # The leaf is opened by basename under the pinned dir FD — arm on
        # the name, whichever spelling the caller used.
        if armed and Path(path).name == target.name and dir_fd is not None:
            armed = False
            target.unlink()
            os.link(sentinel, target)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(evidence.os, "open", raced_open)
    with pytest.raises(evidence.EvidenceError):
        evidence._write_private(target, b"new evidence")
    assert sentinel.read_text() == "KEEP THIS"
    assert armed is False  # the injection really fired


def test_inode_swap_between_lstat_and_open_refused(tmp_path, monkeypatch):
    """A swap to a DIFFERENT singly-linked owned file (the nlink check alone
    would pass) is caught by the inode identity check — both files
    preserved."""
    target = tmp_path / "out.diff"
    target.write_text("old evidence")
    other = tmp_path / "other.diff"
    other.write_text("other evidence")
    real_open = os.open
    armed = True

    def raced_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal armed
        if armed and Path(path).name == target.name and dir_fd is not None:
            armed = False
            target.unlink()
            os.link(other, target)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(evidence.os, "open", raced_open)
    with pytest.raises(evidence.EvidenceError, match="inode"):
        evidence._write_private(target, b"new evidence")
    assert other.read_text() == "other evidence"
    assert target.read_text() == "other evidence"  # hardlinked name left as-is


def test_parent_dir_swap_never_redirects_write(tmp_path, monkeypatch):
    """The evidence dir is pinned by FD: replacing it with a symlink to a
    foreign directory before the leaf open cannot redirect the write."""
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    d = evidence.evidence_dir(store_dir / "dispatch.db")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    target = d / "d_1.diff"
    real_open = os.open
    armed = True

    def raced_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal armed
        if armed and Path(path).name == target.name and dir_fd is not None:
            armed = False
            # Ancestor swap AFTER the dir FD was pinned — the pinned FD
            # still points at the real dir, so the leaf open lands there.
            d.rename(tmp_path / "real_evidence")
            os.symlink(foreign, d)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(evidence.os, "open", raced_open)
    evidence._write_private(target, b"payload")
    assert not (foreign / target.name).exists()
    assert (tmp_path / "real_evidence" / target.name).read_bytes() == b"payload"


def test_rewrite_of_owned_file_still_works(tmp_path):
    """Positive control: replacing our own singly-linked file is allowed —
    verified first, then truncated via the FD."""
    target = tmp_path / "out.diff"
    target.write_text("old evidence")
    evidence._write_private(target, b"new evidence")
    assert target.read_text() == "new evidence"


def test_symlink_leaf_refused_without_write(tmp_path):
    target = tmp_path / "out.diff"
    real = tmp_path / "real.diff"
    real.write_text("untouched")
    os.symlink(real, target)
    with pytest.raises(evidence.EvidenceError):
        evidence._write_private(target, b"new evidence")
    assert real.read_text() == "untouched"
    assert target.is_symlink()


# -- L3: noncandidate direct preset refused BEFORE the wire -------------------

def test_noncandidate_native_preset_refused_before_submit(env, tmp_path):
    """A bound-but-noncandidate NATIVE preset is an unrelated binding — it
    is refused before submit; the post-execution synthetic bit is not
    approval for it."""
    data = env["policy_data"]
    ws = data["workspaces"]["ws-main"]
    cfg = write_runner_config(
        tmp_path / "runner_cfg2" / "exec.json",
        {"ws-alpha": {
            "root": ws["prepared_worktree"],
            "allowed_actions": [],
            "allowed_presets": ["devin/swe-2-max", "devin/other"],
            "allowed_models": None,
        }},
    )
    data["workspaces"]["ws-main"]["runner_execution_config"] = str(cfg)
    data["execution"]["model"] = "devin/other"  # bound, NOT a route candidate
    env["policy_path"] = write_policy(tmp_path, data)
    tid = _card(env, tmp_path)
    report = _tick(env, tmp_path)
    out = next(r for r in report["results"] if r["task_id"] == tid)
    assert out["action"] == "blocked", out
    assert "refusing to submit" in out["reason"]
    assert "not a candidate of the classified route" in out["reason"]
    assert not [r for r in env["stub"].requests
                if r["path"] == "/v1/chat/completions"]
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        == "blocked"


def test_mock_fixture_lane_reaches_wire_but_stays_synthetic(env, tmp_path):
    """The declared mock fixture is the ONLY noncandidate allowed to the
    wire — it submits, then must still complete as the self-reported
    synthetic lane (the stub does not self-report, so it blocks honestly
    AFTER the wire — the exception was exercised, not bypassed)."""
    env["policy_data"]["execution"]["model"] = "mock/text"
    env["policy_path"] = write_policy(tmp_path, env["policy_data"])
    tid = _card(env, tmp_path)
    report = _tick(env, tmp_path)
    out = next(r for r in report["results"] if r["task_id"] == tid)
    submits = [r for r in env["stub"].requests
               if r["path"] == "/v1/chat/completions"]
    assert len(submits) == 1  # the mock exception did reach the wire
    assert submits[0]["body"]["model"] == "mock/text"
    assert out["action"] == "blocked", out
    assert "unverifiable binding" in out["reason"]
