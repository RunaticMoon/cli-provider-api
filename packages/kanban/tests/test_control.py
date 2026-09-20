"""Control-path tests — real Hermes temp board via the bridge, real
loopback stub HTTP for the wrapper. Requires the installed Hermes
interpreter."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from cli_provider_kanban.control import (
    ControlError,
    cmd_accept,
    cmd_approve,
    cmd_cancel,
    cmd_deny,
    cmd_resolve,
    cmd_status,
)
from cli_provider_kanban.dispatch import dispatch_once
from cli_provider_kanban.kernel import KernelBridge
from cli_provider_kanban.store import DispatchStore

from conftest import (
    CURRENT_OS_USER,
    dispatch_policy,  # noqa: F401  (fixture)
    requires_hermes,
    spec_dict,
    stub_wrapper,  # noqa: F401  (fixture)
    write_policy,
)
from test_dispatch import _spec_for, HERMES_ENV_KEYS

pytestmark = requires_hermes


@pytest.fixture
def env(tmp_path, monkeypatch, dispatch_policy, stub_wrapper):
    """Board + bridge + store + policy pointed at the stub wrapper."""
    for key in HERMES_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    data, rev = dispatch_policy
    data["execution"]["base_url"] = stub_wrapper.base_url
    db = tmp_path / "kanban.db"
    bridge = KernelBridge(
        db, env_extra={"HERMES_HOME": str(tmp_path / "hermes_home")}
    )
    store_path = tmp_path / "dispatch.db"
    yield {
        "db": db, "bridge": bridge, "store_path": store_path,
        "policy_data": data, "rev": rev, "stub": stub_wrapper,
        "policy_path": None,
    }
    bridge.close()


def _policy_path(env, tmp_path):
    if env["policy_path"] is None:
        env["policy_path"] = write_policy(tmp_path, env["policy_data"])
    return env["policy_path"]


def _dispatch_card(env, tmp_path, *, spec_over=None):
    """Create a ready card, map its spec, run one tick.

    Returns (task_id, outcome, receipt-or-None) — approval-gated cards get
    an approval record, not a dispatch receipt."""
    data, rev = env["policy_data"], env["rev"]
    tid = env["bridge"].call(
        "create_task", title="c", assignee="jev-native", body="x",
    )["task_id"]
    spec = _spec_for(tid, rev, **(spec_over or {}))
    del spec["task_id"]
    tm = tmp_path / f"tm_{tid}.json"
    tm.write_text(json.dumps({tid: spec}))
    env["policy_data"]["task_map"] = str(tm)
    env["policy_path"] = write_policy(tmp_path, env["policy_data"])
    report = dispatch_once(board_db=env["db"], policy_path=env["policy_path"],
                           store_path=env["store_path"])
    outcome = next(r for r in report["results"] if r["task_id"] == tid)
    receipts = DispatchStore(env["store_path"]).for_task(tid)
    return tid, outcome, (receipts[-1] if receipts else None)


def test_status_reconciles(env, tmp_path):
    tid, _outcome, receipt = _dispatch_card(env, tmp_path)
    out = cmd_status(store_path=env["store_path"], board_db=env["db"],
                     policy_path=_policy_path(env, tmp_path),
                     dispatch_id=receipt.dispatch_id)
    assert out["receipt"]["state"] == "review"
    assert out["task_status"] == "review"
    assert out["run"]["run_id"] == receipt.run_id


def test_cancel_persisted_before_wrapper_call(env, tmp_path):
    """Cancel intent lands on the receipt FIRST (cancel_requested) and the
    wrapper run is cancelled; a card already in review is pulled back and
    blocked — a cancelled execution must not stay promotable."""
    tid, _outcome, receipt = _dispatch_card(env, tmp_path)
    out = cmd_cancel(store_path=env["store_path"], board_db=env["db"],
                     policy_path=_policy_path(env, tmp_path),
                     dispatch_id=receipt.dispatch_id, actor=CURRENT_OS_USER)
    assert out["cancel_requested"] is True
    assert out["wrapper"]["status"] == "cancelled"
    res = DispatchStore(env["store_path"]).get(receipt.dispatch_id)
    assert res.cancel_requested
    # The run genuinely reached review before cancel — the receipt keeps the
    # truthful state, while the CARD is pulled back + blocked.
    assert res.state == "review"
    # accept is disabled outright — never a completion path.
    with pytest.raises(ControlError, match="accept is disabled"):
        cmd_accept(store_path=env["store_path"], board_db=env["db"],
                   policy_path=_policy_path(env, tmp_path),
                   task_id=tid, actor=CURRENT_OS_USER,
                   integrated_revision=env["rev"])
    # Card pulled out of review and blocked — never completed.
    status = env["bridge"].call("get_task", task_id=tid)["task"]["status"]
    assert status in ("blocked", "ready", "todo")
    assert status != "review"


def test_cancel_unauthorized_actor(env, tmp_path):
    tid, _outcome, receipt = _dispatch_card(env, tmp_path)
    with pytest.raises(ControlError, match="not a configured operator"):
        cmd_cancel(store_path=env["store_path"], board_db=env["db"],
                   policy_path=_policy_path(env, tmp_path),
                   dispatch_id=receipt.dispatch_id, actor="mallory")
    assert not DispatchStore(env["store_path"]).get(
        receipt.dispatch_id).cancel_requested


def test_approve_consumes_once_and_unblocks(env, tmp_path):
    data, rev = env["policy_data"], env["rev"]
    data["routes"]["worker.code.hard"] = {"candidates": ["devin-swe-2-max"]}
    tid, outcome, _receipt = _dispatch_card(env, tmp_path, spec_over={"tier": "hard"})
    store = DispatchStore(env["store_path"])
    ap = store.pending_approval_for(tid, "dispatch")
    assert ap is not None
    store.close()
    out = cmd_approve(store_path=env["store_path"], board_db=env["db"],
                    policy_path=_policy_path(env, tmp_path),
                    approval_id=ap.approval_id, actor=CURRENT_OS_USER)
    assert out["state"] == "applied"
    # Card unblocked -> ready for the next tick.
    assert out["task_status"] in ("ready", "todo")
    # Consume-once: second apply refuses.
    with pytest.raises(ControlError):
        cmd_approve(store_path=env["store_path"], board_db=env["db"],
                    policy_path=_policy_path(env, tmp_path),
                    approval_id=ap.approval_id, actor=CURRENT_OS_USER)


def test_approve_unauthorized(env, tmp_path):
    data, rev = env["policy_data"], env["rev"]
    data["routes"]["worker.code.hard"] = {"candidates": ["devin-swe-2-max"]}
    tid, outcome, _r = _dispatch_card(env, tmp_path, spec_over={"tier": "hard"})
    store = DispatchStore(env["store_path"])
    ap = store.pending_approval_for(tid, "dispatch")
    store.close()
    with pytest.raises(ControlError):
        cmd_approve(store_path=env["store_path"], board_db=env["db"],
                    policy_path=_policy_path(env, tmp_path),
                    approval_id=ap.approval_id, actor="mallory")


def test_deny_approval(env, tmp_path):
    data, rev = env["policy_data"], env["rev"]
    data["routes"]["worker.code.hard"] = {"candidates": ["devin-swe-2-max"]}
    tid, outcome, _r = _dispatch_card(env, tmp_path, spec_over={"tier": "hard"})
    store = DispatchStore(env["store_path"])
    ap = store.pending_approval_for(tid, "dispatch")
    store.close()
    out = cmd_deny(store_path=env["store_path"],
                   policy_path=_policy_path(env, tmp_path),
                   approval_id=ap.approval_id, actor=CURRENT_OS_USER)
    assert out["state"] == "denied"
    # Card stays blocked.
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        == "blocked"


def test_accept_is_always_disabled(env, tmp_path):
    """accept is disabled in this slice — a review-state card plus a
    caller-supplied 40-hex string is not proof of an integrated revision;
    the Lead completes via Hermes directly."""
    tid, _outcome, receipt = _dispatch_card(env, tmp_path)
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        == "review"
    with pytest.raises(ControlError, match="accept is disabled"):
        cmd_accept(store_path=env["store_path"], board_db=env["db"],
                   policy_path=_policy_path(env, tmp_path),
                   task_id=tid, actor=CURRENT_OS_USER,
                   integrated_revision=env["rev"])
    # Never completes — the card stays in review for the Lead.
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        == "review"


def test_accept_disabled_for_non_review_card(env, tmp_path):
    tid = env["bridge"].call(
        "create_task", title="c", assignee="jev-native", body="x",
    )["task_id"]
    with pytest.raises(ControlError, match="accept is disabled"):
        cmd_accept(store_path=env["store_path"], board_db=env["db"],
                   policy_path=_policy_path(env, tmp_path),
                   task_id=tid, actor=CURRENT_OS_USER,
                   integrated_revision=env["rev"])


def test_resolve_unknown_completed_goes_to_review(env, tmp_path):
    """An unknown receipt whose wrapper run completed resolves to review —
    but only after the SAME verification bar (bound worktree + trusted argv
    + spec) as the normal path."""
    data, rev = env["policy_data"], env["rev"]
    tid = env["bridge"].call(
        "create_task", title="c", assignee="jev-native", body="x",
    )["task_id"]
    spec = _spec_for(tid, rev)
    del spec["task_id"]
    tm = tmp_path / f"tm_{tid}.json"
    tm.write_text(json.dumps({tid: spec}))
    env["policy_data"]["task_map"] = str(tm)
    env["policy_path"] = write_policy(tmp_path, env["policy_data"])
    # Simulate a crashed-after-submit: run exists, receipt unknown, and the
    # bound worktree is recorded on the receipt.
    from cli_provider_kanban.wrapper_client import WrapperClient
    client = WrapperClient(env["stub"].base_url)
    out = client.submit_chat(model="m", task_id=tid, workspace_id="ws-alpha",
                             messages=[])
    prepared = data["workspaces"]["ws-main"]["prepared_worktree"]
    store = DispatchStore(env["store_path"])
    res = store.reserve(task_id=tid, task_revision="1",
                        spec_hash="sha256:x", policy_fingerprint="sha256:p",
                        workspace_id="ws-main", base_revision=rev,
                        route="worker.code.standard")
    store.transition(res.dispatch_id, "submitted", run_id=out.run_id,
                     attempt_id=out.attempt_id,
                     worktree=prepared, branch="jev/prepared-ws-main")
    store.transition(res.dispatch_id, "unknown",
                     detail="crashed after submit")
    store.close()

    out = cmd_resolve(store_path=env["store_path"], board_db=env["db"],
                      policy_path=_policy_path(env, tmp_path),
                      dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    assert out["resolved"] == "review"
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        == "review"


def test_resolve_unknown_no_run_stays_unknown(env, tmp_path):
    """No wrapper run on record is NOT proof nothing executed — the
    receipt stays unknown pending external operator investigation."""
    data, rev = env["policy_data"], env["rev"]
    tid = env["bridge"].call(
        "create_task", title="c", assignee="jev-native", body="x",
    )["task_id"]
    store = DispatchStore(env["store_path"])
    res = store.reserve(task_id=tid, task_revision="1",
                        spec_hash="sha256:x", policy_fingerprint="sha256:p",
                        workspace_id="ws-main", base_revision=rev,
                        route="worker.code.standard")
    store.transition(res.dispatch_id, "unknown", detail="lost")
    store.close()
    out = cmd_resolve(store_path=env["store_path"], board_db=env["db"],
                      policy_path=_policy_path(env, tmp_path),
                      dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    assert out["resolved"] == "none"
    store = DispatchStore(env["store_path"])
    assert store.get(res.dispatch_id).state == "unknown"
    store.close()
