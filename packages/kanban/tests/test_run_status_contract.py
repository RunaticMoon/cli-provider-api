"""Terminal-status contract — only completed+succeeded may reach review.

Parent-identified defect: ``_handle_run_status`` treated ANY unrecognized or
still-live status as completed and sent the card to review. The fix admits a
run to verification only when the canonical wrapper view says exactly
``status == "completed"`` AND ``outcome == "succeeded"`` AND the run's
task/workspace/execution context equals the reserved expected context;
everything else holds or typed-blocks.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from cli_provider_kanban.dispatch import dispatch_once
from cli_provider_kanban.store import DispatchStore

from conftest import (  # noqa: F401
    FakeWrapper,
    dispatch_policy,
    requires_hermes,
    write_policy,
)
from test_dispatch import HERMES_ENV_KEYS, _spec_for, board  # noqa: F401

pytestmark = requires_hermes


def _card(bridge, tmp_path, data, rev, **spec_over):
    tid = bridge.call("create_task", title="c", assignee="jev-native",
                      body="x")["task_id"]
    spec = _spec_for(tid, rev, **spec_over)
    del spec["task_id"]
    tm = tmp_path / f"tm_{tid}.json"
    tm.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(tm)
    return tid


def _run(board, tmp_path, data, client):
    policy_path = write_policy(tmp_path, data)
    store = tmp_path / "dispatch.db"
    report = dispatch_once(
        board_db=board, policy_path=policy_path, store_path=store,
        client=client,
    )
    return report, store


def _result(report, tid):
    return next(r for r in report["results"] if r["task_id"] == tid)


def test_live_status_holds_never_reviews(board, tmp_path, dispatch_policy):
    """A still-running view at tick end must not promote to review."""
    db, bridge = board
    data, rev = dispatch_policy
    tid = _card(bridge, tmp_path, data, rev)
    client = FakeWrapper(status="running")
    report, store = _run(db, tmp_path, data, client)
    rec = _result(report, tid)
    assert rec["action"] not in ("review", "dispatched") or \
        not rec.get("dispatched")
    assert rec["action"] == "in_flight"
    task = bridge.call("get_task", task_id=tid)["task"]
    assert task["status"] == "blocked"
    # Receipt stays live (submitted) — never a terminal promotion, and it
    # permanently blocks blind re-dispatch until `control resolve`.
    receipt = DispatchStore(store).for_task(tid)[0]
    assert receipt.state == "submitted"


def test_unrecognized_status_holds_never_reviews(
    board, tmp_path, dispatch_policy
):
    db, bridge = board
    data, rev = dispatch_policy
    tid = _card(bridge, tmp_path, data, rev)
    client = FakeWrapper(status="detonating")  # nonsense status
    report, store = _run(db, tmp_path, data, client)
    rec = _result(report, tid)
    assert rec["action"] == "in_flight"
    assert bridge.call("get_task", task_id=tid)["task"]["status"] == "blocked"
    assert DispatchStore(store).for_task(tid)[0].state == "submitted"


def test_completed_without_succeeded_outcome_blocked(
    board, tmp_path, dispatch_policy
):
    """status=completed but outcome=partial is NOT a success candidate."""
    db, bridge = board
    data, rev = dispatch_policy
    tid = _card(bridge, tmp_path, data, rev)
    client = FakeWrapper(status="completed", outcome="partial")
    report, store = _run(db, tmp_path, data, client)
    rec = _result(report, tid)
    assert rec["action"] == "blocked"
    assert bridge.call("get_task", task_id=tid)["task"]["status"] == "blocked"
    assert DispatchStore(store).for_task(tid)[0].state in (
        "blocked", "failed")


def test_completed_missing_outcome_blocked(board, tmp_path, dispatch_policy):
    db, bridge = board
    data, rev = dispatch_policy
    tid = _card(bridge, tmp_path, data, rev)
    client = FakeWrapper(status="completed", outcome=None,
                         run_overrides={"outcome": None})
    report, store = _run(db, tmp_path, data, client)
    rec = _result(report, tid)
    assert rec["action"] == "blocked"
    assert bridge.call("get_task", task_id=tid)["task"]["status"] == "blocked"


def test_task_id_mismatch_never_verifies(board, tmp_path, dispatch_policy):
    """A run view for a different task id is not our reserved context —
    the run is anomalous (unknown), never verified, never reviewed."""
    db, bridge = board
    data, rev = dispatch_policy
    tid = _card(bridge, tmp_path, data, rev)
    client = FakeWrapper(run_overrides={"task_id": "t_other"})
    report, store = _run(db, tmp_path, data, client)
    rec = _result(report, tid)
    assert rec["action"] == "unknown"
    task = bridge.call("get_task", task_id=tid)["task"]
    assert task["status"] == "blocked"
    assert DispatchStore(store).for_task(tid)[0].state == "unknown"


def test_workspace_mismatch_never_verifies(board, tmp_path, dispatch_policy):
    db, bridge = board
    data, rev = dispatch_policy
    tid = _card(bridge, tmp_path, data, rev)
    client = FakeWrapper(run_overrides={"workspace_id": "ws-elsewhere"})
    report, store = _run(db, tmp_path, data, client)
    rec = _result(report, tid)
    assert rec["action"] == "unknown"
    assert DispatchStore(store).for_task(tid)[0].state == "unknown"


def test_execution_echo_mismatch_never_verifies(
    board, tmp_path, dispatch_policy
):
    """The echoed execution metadata must equal what we reserved against."""
    db, bridge = board
    data, rev = dispatch_policy
    tid = _card(bridge, tmp_path, data, rev)
    client = FakeWrapper(run_overrides={
        "execution": {"task_revision": "1", "base_revision": "0" * 40,
                      "route": "worker.code.easy",
                      "policy_version": "forged"},
    })
    report, store = _run(db, tmp_path, data, client)
    rec = _result(report, tid)
    assert rec["action"] == "unknown"
    assert DispatchStore(store).for_task(tid)[0].state == "unknown"


def test_happy_path_still_reviews(board, tmp_path, dispatch_policy):
    """completed + succeeded + matching context still reaches review."""
    db, bridge = board
    data, rev = dispatch_policy
    tid = _card(bridge, tmp_path, data, rev)
    client = FakeWrapper()
    report, store = _run(db, tmp_path, data, client)
    rec = _result(report, tid)
    assert rec["action"] == "review", rec
    assert bridge.call("get_task", task_id=tid)["task"]["status"] == "review"
