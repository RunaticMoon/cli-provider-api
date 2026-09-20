"""Sidecar receipt store: reservation uniqueness + approval lifecycle."""

from __future__ import annotations

import time

import pytest

from cli_provider_kanban.store import (
    ABORTED,
    CLAIMED,
    REVIEW,
    SUBMITTED,
    UNKNOWN,
    DispatchStore,
    ReservationExists,
    StoreError,
)


@pytest.fixture
def store(tmp_path):
    s = DispatchStore(tmp_path / "dispatch.db")
    yield s
    s.close()


def _reserve(store, task_id="t_1", workspace="ws-main", **kw):
    return store.reserve(
        task_id=task_id,
        task_revision="1",
        spec_hash="sha256:x",
        policy_fingerprint="sha256:p",
        workspace_id=workspace,
        base_revision="0" * 40,
        route="worker.code.standard",
        **kw,
    )


def test_reserve_and_get(store):
    res = _reserve(store)
    assert res.dispatch_id.startswith("d_")
    assert res.state == "reserved"
    assert store.get(res.dispatch_id).task_id == "t_1"


def test_one_live_reservation_per_card(store):
    _reserve(store, task_id="t_dup")
    with pytest.raises(ReservationExists):
        _reserve(store, task_id="t_dup")


def test_terminal_reservation_frees_the_card(store):
    res = _reserve(store, task_id="t_x")
    store.transition(res.dispatch_id, ABORTED)
    res2 = _reserve(store, task_id="t_x")
    assert res2.dispatch_id != res.dispatch_id


def test_unknown_reservation_blocks_redispatch(store):
    res = _reserve(store, task_id="t_unk")
    store.transition(res.dispatch_id, CLAIMED)
    store.transition(res.dispatch_id, SUBMITTED, run_id="run_1")
    store.transition(res.dispatch_id, UNKNOWN, detail="crash")
    with pytest.raises(ReservationExists):
        _reserve(store, task_id="t_unk")


def test_unknown_blocks_workspace_too(store):
    _reserve(store, task_id="t_a", workspace="ws-main")
    store.transition(store.for_task("t_a")[0].dispatch_id, UNKNOWN)
    with pytest.raises(ReservationExists):
        _reserve(store, task_id="t_b", workspace="ws-main")


def test_second_workspace_allowed(store):
    _reserve(store, task_id="t_a", workspace="ws-main")
    res = _reserve(store, task_id="t_b", workspace="ws-other")
    assert res.workspace_id == "ws-other"


def test_terminal_state_is_one_way(store):
    res = _reserve(store)
    store.transition(res.dispatch_id, ABORTED)
    with pytest.raises(StoreError):
        store.transition(res.dispatch_id, CLAIMED)


def test_request_cancel_persists(store):
    res = _reserve(store)
    updated = store.request_cancel(res.dispatch_id)
    assert updated.cancel_requested is True
    # Survives "restart" (fresh store object on the same file).
    reopened = DispatchStore(store.path)
    try:
        assert reopened.get(res.dispatch_id).cancel_requested is True
    finally:
        reopened.close()


def test_store_survives_restart(store):
    res = _reserve(store, task_id="t_persist")
    store.transition(res.dispatch_id, SUBMITTED, run_id="run_9")
    store.close()
    reopened = DispatchStore(store.path)
    try:
        live = reopened.live_for_task("t_persist")
        assert live is not None and live.run_id == "run_9"
    finally:
        reopened.close()


# --- approvals ---------------------------------------------------------------


def _approval(store, ttl=3600, actors=("op-test",)):
    return store.create_approval(
        task_id="t_1",
        task_revision="1",
        spec_hash="sha256:x",
        policy_fingerprint="sha256:p",
        operation="dispatch",
        run_id=None,
        allowed_actors=list(actors),
        ttl_seconds=ttl,
    )


def test_approval_apply_once(store):
    ap = _approval(store)
    assert ap.state == "pending"
    applied = store.apply_approval(ap.approval_id, "op-test")
    assert applied.state == "applied"
    assert applied.decided_by == "op-test"
    with pytest.raises(StoreError, match="exactly once|already"):
        store.apply_approval(ap.approval_id, "op-test")


def test_approval_deny_once(store):
    ap = _approval(store)
    denied = store.deny_approval(ap.approval_id, "op-test")
    assert denied.state == "denied"
    with pytest.raises(StoreError):
        store.apply_approval(ap.approval_id, "op-test")


def test_approval_expiry_denies(store):
    ap = _approval(store, ttl=-1)  # already expired
    with pytest.raises(StoreError, match="expired"):
        store.apply_approval(ap.approval_id, "op-test")
    assert store.get_approval(ap.approval_id).state == "expired"


def test_approval_unauthorized_actor(store):
    ap = _approval(store, actors=("op-test",))
    with pytest.raises(StoreError, match="allowlist"):
        store.apply_approval(ap.approval_id, "mallory")
    # Still pending — unauthorized attempt does not consume.
    assert store.get_approval(ap.approval_id).state == "pending"


def test_approval_requires_actors(store):
    with pytest.raises(StoreError, match="allowlist|actors"):
        _approval(store, actors=())


def test_pending_approval_lookup(store):
    ap = _approval(store)
    found = store.pending_approval_for("t_1", "dispatch")
    assert found is not None and found.approval_id == ap.approval_id
    store.apply_approval(ap.approval_id, "op-test")
    assert store.pending_approval_for("t_1", "dispatch") is None
