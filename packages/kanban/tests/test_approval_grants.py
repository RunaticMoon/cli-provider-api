"""Approval grant lifecycle — bound to exact scope, consumed atomically once.

Parent-identified defect: an *applied* approval unblocked the card, but the
next tick re-gated the unchanged card — an infinite pending loop — because
the approval was not bound to the spec/policy scope nor consumed at
reservation time. Grants now bind (task revision + spec hash + policy
fingerprint + operation + expiry) and are consumed atomically with the
dispatch reservation.
"""

from __future__ import annotations

import json
import time

import pytest

from cli_provider_kanban.control import cmd_approve
from cli_provider_kanban.dispatch import dispatch_once
from cli_provider_kanban.store import DispatchStore

from conftest import (  # noqa: F401
    CURRENT_OS_USER,
    FakeWrapper,
    dispatch_policy,
    requires_hermes,
    write_policy,
)
from test_dispatch import HERMES_ENV_KEYS, _spec_for, board  # noqa: F401

pytestmark = requires_hermes


def _gated_card(bridge, tmp_path, data, rev, **spec_over):
    """A hard-tier card: needs_approval on every tick until granted."""
    data["routes"]["worker.code.hard"] = {"candidates": ["devin-swe-2-max"]}
    tid = bridge.call("create_task", title="gated", assignee="jev-native",
                      body="x")["task_id"]
    spec = _spec_for(tid, rev, tier="hard", **spec_over)
    del spec["task_id"]
    tm = tmp_path / f"tm_{tid}.json"
    tm.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(tm)
    return tid


def _tick(board, tmp_path, data, client=None):
    policy_path = write_policy(tmp_path, data)
    store = tmp_path / "dispatch.db"
    report = dispatch_once(
        board_db=board, policy_path=policy_path, store_path=store,
        client=client or FakeWrapper(),
    )
    return report, store, policy_path


def test_pending_then_approved_dispatches_exactly_once(
    board, tmp_path, dispatch_policy
):
    db, bridge = board
    data, rev = dispatch_policy
    tid = _gated_card(bridge, tmp_path, data, rev)

    client = FakeWrapper()
    report, store, policy_path = _tick(db, tmp_path, data, client)
    rec = next(r for r in report["results"] if r["task_id"] == tid)
    assert rec["action"] == "needs_approval"
    assert client.calls == []

    out = cmd_approve(store_path=store, board_db=db, policy_path=policy_path,
                      approval_id=rec["approval_id"], actor=CURRENT_OS_USER)
    assert out["state"] == "applied"

    # Next tick consumes the grant — real dispatch, exactly once.
    report2, store, _ = _tick(db, tmp_path, data, client)
    rec2 = next(r for r in report2["results"] if r["task_id"] == tid)
    assert rec2["action"] == "review", rec2
    assert len(client.calls) == 1
    s = DispatchStore(store)
    ap = s.get_approval(rec["approval_id"])
    assert ap.state == "consumed"
    assert len(s.for_task(tid)) == 1
    s.close()
    assert bridge.call("get_task", task_id=tid)["task"]["status"] == "review"


def test_changed_spec_invalidates_grant(board, tmp_path, dispatch_policy):
    """Edit the card body after approval -> the grant no longer matches the
    scope; a NEW approval is required (no dispatch, no consumption)."""
    db, bridge = board
    data, rev = dispatch_policy
    tid = _gated_card(bridge, tmp_path, data, rev)
    client = FakeWrapper()
    report, store, policy_path = _tick(db, tmp_path, data, client)
    rec = next(r for r in report["results"] if r["task_id"] == tid)
    cmd_approve(store_path=store, board_db=db, policy_path=policy_path,
                approval_id=rec["approval_id"], actor=CURRENT_OS_USER)

    # Card body changed -> new task_revision/spec_hash.
    spec = _spec_for(tid, rev, tier="hard", task_revision="2",
                     objective="a different objective")
    del spec["task_id"]
    tm = tmp_path / f"tm_{tid}.json"
    tm.write_text(json.dumps({tid: spec}))

    report2, store, _ = _tick(db, tmp_path, data, client)
    rec2 = next(r for r in report2["results"] if r["task_id"] == tid)
    assert rec2["action"] == "needs_approval"
    assert client.calls == []
    s = DispatchStore(store)
    assert s.get_approval(rec["approval_id"]).state == "applied"  # not consumed
    s.close()


def test_changed_policy_invalidates_grant(board, tmp_path, dispatch_policy):
    db, bridge = board
    data, rev = dispatch_policy
    tid = _gated_card(bridge, tmp_path, data, rev)
    client = FakeWrapper()
    report, store, policy_path = _tick(db, tmp_path, data, client)
    rec = next(r for r in report["results"] if r["task_id"] == tid)
    cmd_approve(store_path=store, board_db=db, policy_path=policy_path,
                approval_id=rec["approval_id"], actor=CURRENT_OS_USER)

    data["policy_version"] = "2026-09-20.2"   # fingerprint changes
    report2, store, _ = _tick(db, tmp_path, data, client)
    rec2 = next(r for r in report2["results"] if r["task_id"] == tid)
    assert rec2["action"] == "needs_approval"
    assert client.calls == []


def test_expired_approval_denies(board, tmp_path, dispatch_policy):
    db, bridge = board
    data, rev = dispatch_policy
    data["approval"]["expiry_seconds"] = 1
    tid = _gated_card(bridge, tmp_path, data, rev)
    client = FakeWrapper()
    report, store, policy_path = _tick(db, tmp_path, data, client)
    rec = next(r for r in report["results"] if r["task_id"] == tid)
    store_obj = DispatchStore(store)
    ap = store_obj.get_approval(rec["approval_id"])
    # Force expiry.
    store_obj._conn.execute(
        "UPDATE approvals SET expires_at = ? WHERE approval_id = ?",
        (time.time() - 1, ap.approval_id))
    store_obj._conn.commit()
    store_obj.close()
    from cli_provider_kanban.control import ControlError
    with pytest.raises(ControlError):
        cmd_approve(store_path=store, board_db=db, policy_path=policy_path,
                    approval_id=ap.approval_id, actor=CURRENT_OS_USER)


def test_approval_never_overrides_effort_gate(board, tmp_path, dispatch_policy):
    """Approval cannot override an unverifiable effort mapping — the gate
    fires before the grant is consumed and the grant stays applied."""
    db, bridge = board
    data, rev = dispatch_policy
    tid = _gated_card(bridge, tmp_path, data, rev, effort_hint="economy")
    client = FakeWrapper()
    report, store, policy_path = _tick(db, tmp_path, data, client)
    rec = next(r for r in report["results"] if r["task_id"] == tid)
    assert rec["action"] == "needs_approval"
    cmd_approve(store_path=store, board_db=db, policy_path=policy_path,
                approval_id=rec["approval_id"], actor=CURRENT_OS_USER)

    report2, store, _ = _tick(db, tmp_path, data, client)
    rec2 = next(r for r in report2["results"] if r["task_id"] == tid)
    # Economy hint maps to nothing verifiable -> blocked, never executed.
    assert rec2["action"] == "blocked"
    assert "effort" in rec2["reason"]
    assert client.calls == []
    s = DispatchStore(store)
    assert s.get_approval(rec["approval_id"]).state == "applied"  # not burned
    s.close()


def test_non_auto_effort_fails_closed(board, tmp_path, dispatch_policy):
    """Non-auto effort hints have no verified path to the pinned Runner —
    dispatch must fail closed rather than silently run the wrong effort."""
    db, bridge = board
    data, rev = dispatch_policy
    tid = bridge.call("create_task", title="c", assignee="jev-native",
                      body="x")["task_id"]
    spec = _spec_for(tid, rev, effort_hint="balanced")
    del spec["task_id"]
    tm = tmp_path / f"tm_{tid}.json"
    tm.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(tm)
    client = FakeWrapper()
    report, store, _ = _tick(db, tmp_path, data, client)
    rec = next(r for r in report["results"] if r["task_id"] == tid)
    assert rec["action"] == "blocked"
    assert "effort" in rec["reason"]
    assert client.calls == []


def test_unrelated_task_not_blocked_by_pending(board, tmp_path,
                                               dispatch_policy):
    """A pending approval on one card must not block a clean card."""
    db, bridge = board
    data, rev = dispatch_policy
    gated = _gated_card(bridge, tmp_path, data, rev)
    # A second standard-tier card on a second workspace.
    data["workspaces"]["ws-other"] = dict(data["workspaces"]["ws-main"])
    clean = bridge.call("create_task", title="clean", assignee="jev-native",
                        body="x")["task_id"]
    spec = _spec_for(clean, rev, workspace_id="ws-other")
    del spec["task_id"]
    tm = tmp_path / "tm2.json"
    tm.write_text(json.dumps({clean: spec}))
    # Merge both cards into one map.
    import json as _j
    merged = _j.loads((tmp_path / f"tm_{gated}.json").read_text())
    merged[clean] = spec
    tm.write_text(_j.dumps(merged))
    data["task_map"] = str(tm)

    client = FakeWrapper()
    report, store, _ = _tick(db, tmp_path, data, client)
    actions = {r["task_id"]: r["action"] for r in report["results"]}
    assert actions[gated] == "needs_approval"
    assert actions[clean] == "review"
