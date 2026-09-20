"""Durable cross-candidate retry-safety contract (jev-router slice).

Once an attempt may have reached the Runner (dispatch marker ``started_at`` is
persisted BEFORE the run RPC) the task is never silently retried — not under the
same preset, not under a different preset, not after an API restart. The ONLY
admissible fallback is an attempt that is proven pre-execution:

* ``failed`` with outcome ``rejected`` or ``queue_timeout`` (never dispatched, or
  the Runner's typed pre-execution rejection attesting the driver never ran), or
* ``cancelled`` with no dispatch marker (cancelled while queued).

``completed`` replays the cached result under the same preset; it never executes
again. Everything else — provider failure, dispatched cancel, unknown, partial
content races — is a durable ``run_not_retryable`` conflict.
"""

from __future__ import annotations

import asyncio

import pytest

from cli_provider_core import Conflict, Store
from cli_provider_core.models import (
    CANCELLED,
    CANCELLING,
    COMPLETED,
    FAILED,
    LOCK_STATUSES,
    OUTCOME_CANCELLED,
    OUTCOME_PROVIDER_ERROR,
    OUTCOME_REJECTED,
    OUTCOME_SUCCEEDED,
    OUTCOME_QUEUE_TIMEOUT,
    OUTCOME_UNKNOWN,
    QUEUED,
    RESERVED,
    RUNNING,
    STARTING,
    UNKNOWN,
)
from cli_provider_core.store import proven_pre_execution
from cli_provider_sdk import (
    CompletionStatus,
    EventKind,
    Outcome,
    RunResult,
    Usage,
    UsageProvenance,
    Verification,
    VerificationStatus,
)
from conftest import FakeControl, FakeSession, make_system
from test_controller import prepared, submit


class _PreExecutionRejectSession(FakeSession):
    """Runner double whose typed rejection is proven to have no effect."""

    async def run(self, params):
        from cli_provider_core.errors import RunnerRunRejected

        raise RunnerRunRejected(
            "runner run error QUEUE_FULL",
            runner_code="QUEUE_FULL",
            retryable=True,
            stage="pre_execution",
        )
        yield  # pragma: no cover - async generator


async def test_provider_failure_is_never_retried_same_or_other_preset(tmp_path):
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "failed"
    first = await submit(controller, registry, task="task-f")
    record = await first.active.task
    assert record.status == FAILED and record.outcome == OUTCOME_PROVIDER_ERROR

    for preset in ("mock/text", "mock/text-beta"):
        with pytest.raises(Conflict) as excinfo:
            await submit(controller, registry, task="task-f", preset=preset)
        assert excinfo.value.code == "run_not_retryable"
        assert excinfo.value.run_id == record.run_id
    assert control.executions == 1
    store.close()


async def test_dispatched_cancel_is_never_retried(tmp_path):
    """A cancel that reached dispatch may have had driver effects."""
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "hang"
    sub = await submit(controller, registry, task="task-c")
    await asyncio.sleep(0.05)
    view = await controller.cancel(sub.record.run_id, "alpha")
    assert view.confirmed is True
    record = store.get_attempt(sub.record.run_id)
    assert record.status == CANCELLED and record.started_at is not None

    with pytest.raises(Conflict) as excinfo:
        await submit(controller, registry, task="task-c", preset="mock/text-beta")
    assert excinfo.value.code == "run_not_retryable"
    assert control.executions == 1
    store.close()


async def test_queue_timeout_is_proven_pre_execution_and_admits_fallback(tmp_path):
    """Positive control: queue admission timeout never dispatched, so the SAME
    task may fall back once capacity exists."""
    config, store, registry, controller, control = await prepared(
        tmp_path,
        api={
            "concurrency": {
                "per_runner": 1,
                "per_principal": 2,
                "queue_timeout_seconds": 0.2,
            }
        },
    )
    control.behavior = "hang"
    first = await submit(controller, registry, task="task-q1")
    await asyncio.sleep(0.05)
    second = await submit(controller, registry, task="task-q2")
    record = await second.active.task
    assert record.status == FAILED and record.outcome == OUTCOME_QUEUE_TIMEOUT
    assert record.started_at is None

    await controller.cancel(first.record.run_id, "alpha")
    third = await submit(controller, registry, task="task-q2")
    assert third.cached is False
    assert third.record.run_id != record.run_id
    await third.active.task  # settles quickly (hang_event already released)
    assert control.executions == 2  # first run + the admitted retry only
    store.close()


async def test_runner_typed_pre_execution_rejection_admits_fallback(tmp_path):
    """The Runner's typed pre-execution rejection is the positive control for
    safe sequential fallback across candidates sharing task identity."""
    config, store, registry, controller, control = await prepared(tmp_path)
    registry._session_factory = lambda cfg: _PreExecutionRejectSession(cfg, control)
    rejected = await submit(controller, registry, task="task-p")
    record = await rejected.active.task
    assert record.status == FAILED and record.outcome == OUTCOME_REJECTED
    assert control.executions == 0

    registry._session_factory = lambda cfg: FakeSession(cfg, control)
    fallback = await submit(
        controller, registry, task="task-p", preset="mock/text-beta"
    )
    result = await fallback.active.task
    assert result.status == COMPLETED
    assert control.executions == 1  # only the fallback executed
    store.close()


async def test_cancelled_while_queued_is_proven_pre_execution(tmp_path):
    """A run cancelled while queued provably never dispatched (no started_at
    marker), so an explicit resubmission is admissible."""
    config, store, registry, controller, control = await prepared(
        tmp_path, api={"concurrency": {"per_runner": 1, "per_principal": 1}}
    )
    control.behavior = "hang"
    first = await submit(controller, registry, task="task-hold")
    await asyncio.sleep(0.05)
    queued = await submit(controller, registry, task="task-qc")
    await asyncio.sleep(0.05)
    view = await controller.cancel(queued.record.run_id, "alpha")
    assert view.confirmed is True
    record = store.get_attempt(queued.record.run_id)
    assert record.status == CANCELLED and record.started_at is None

    await controller.cancel(first.record.run_id, "alpha")
    retry = await submit(controller, registry, task="task-qc")
    assert retry.cached is False
    assert retry.record.run_id != queued.record.run_id
    await controller.cancel(retry.record.run_id, "alpha")
    store.close()


async def test_completed_partial_replays_cached_and_never_reexecutes(tmp_path):
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "partial"
    first = await submit(controller, registry, task="task-partial")
    record = await first.active.task
    assert record.status == COMPLETED and record.outcome == "partial"

    replay = await submit(controller, registry, task="task-partial")
    assert replay.cached is True and replay.record.run_id == record.run_id
    assert control.executions == 1
    store.close()


async def test_sequential_fallback_attempts_cannot_overlap(tmp_path):
    """After a proven pre-execution rejection, a second reservation holds the
    lock; a third concurrent submit conflicts instead of double-dispatching."""
    config, store, registry, controller, control = await prepared(tmp_path)
    registry._session_factory = lambda cfg: _PreExecutionRejectSession(cfg, control)
    rejected = await submit(controller, registry, task="task-seq")
    await rejected.active.task

    control.behavior = "hang"
    registry._session_factory = lambda cfg: FakeSession(cfg, control)
    running = await submit(controller, registry, task="task-seq")
    with pytest.raises(Conflict) as excinfo:
        await submit(controller, registry, task="task-seq")
    assert excinfo.value.code == "run_active"
    await controller.cancel(running.record.run_id, "alpha")
    store.close()


async def test_restart_preserves_not_retryable_and_unknown(tmp_path):
    """The guard is durable in the Store, not an HTTP-layer cache."""
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "failed"
    first = await submit(controller, registry, task="task-f")
    record = await first.active.task
    db_path = store.db_path
    store.close()

    store2 = Store(db_path)
    store2.initialize()
    from cli_provider_core import RunController

    controller2 = RunController(config=config, store=store2, registry=registry)
    with pytest.raises(Conflict) as excinfo:
        await submit(controller2, registry, task="task-f")
    assert excinfo.value.code == "run_not_retryable"
    store2.close()


async def test_dispatch_marker_is_set_before_runner_rpc(tmp_path):
    """``started_at``/STARTING is persisted before the run RPC is issued."""

    class MarkerCheckSession(FakeSession):
        async def run(self, params):
            record = store.get_attempt(params["run_id"])
            assert record is not None
            assert record.status == "starting"
            assert record.started_at is not None
            async for event in super().run(params):
                yield event

    config, store, registry, controller, control = await prepared(tmp_path)
    registry._session_factory = lambda cfg: MarkerCheckSession(cfg, control)
    sub = await submit(controller, registry, task="task-marker")
    record = await sub.active.task
    assert record.status == COMPLETED
    store.close()


# ------------------------------------------------------------- store level


def _reserve(store: Store, run_id: str, *, task="task-1", hash_="sha256:a"):
    return store.reserve(
        run_id=run_id,
        attempt_id=f"att_{run_id}",
        principal="alpha",
        task_id=task,
        preset="mock/text",
        driver_id="mock",
        runner_instance="runner-1",
        workspace_id="ws-alpha",
        request_hash=hash_,
        task_policy="text",
        status=QUEUED,
    )


def test_reserve_blocks_new_attempt_after_completed_same_hash(tmp_path):
    store = Store(str(tmp_path / "core.db"))
    store.initialize()
    _reserve(store, "run_1")
    store.set_attempt("run_1", status=COMPLETED, outcome=OUTCOME_SUCCEEDED)
    with pytest.raises(Conflict) as excinfo:
        _reserve(store, "run_2")
    assert excinfo.value.code == "run_not_retryable"
    store.close()


def test_reserve_blocks_after_provider_failure_and_dispatched_cancel(tmp_path):
    store = Store(str(tmp_path / "core.db"))
    store.initialize()
    _reserve(store, "run_1", task="t-fail")
    store.set_attempt("run_1", status=FAILED, outcome=OUTCOME_PROVIDER_ERROR)
    with pytest.raises(Conflict) as excinfo:
        _reserve(store, "run_2", task="t-fail")
    assert excinfo.value.code == "run_not_retryable"

    _reserve(store, "run_3", task="t-cancel")
    store.set_attempt("run_3", status="starting", started_at="2026-01-01T00:00:00+00:00")
    store.set_attempt("run_3", status=CANCELLED, outcome=OUTCOME_CANCELLED)
    with pytest.raises(Conflict) as excinfo:
        _reserve(store, "run_4", task="t-cancel")
    assert excinfo.value.code == "run_not_retryable"
    store.close()


def test_reserve_admits_only_proven_pre_execution_history(tmp_path):
    store = Store(str(tmp_path / "core.db"))
    store.initialize()
    _reserve(store, "run_1")
    store.set_attempt("run_1", status=FAILED, outcome=OUTCOME_REJECTED)
    _reserve(store, "run_2")
    store.set_attempt("run_2", status=FAILED, outcome=OUTCOME_QUEUE_TIMEOUT)
    _reserve(store, "run_3")
    # cancelled before dispatch: no started_at marker
    store.set_attempt("run_3", status=CANCELLED, outcome=OUTCOME_CANCELLED)
    record = _reserve(store, "run_4")
    assert record.status == QUEUED
    store.close()


def test_reserve_rejection_chain_cannot_skip_a_blocking_ancestor(tmp_path):
    """Hand-seeded history: a blocking attempt anywhere in the chain blocks."""
    store = Store(str(tmp_path / "core.db"))
    store.initialize()
    _reserve(store, "run_1")
    store.set_attempt("run_1", status=FAILED, outcome=OUTCOME_PROVIDER_ERROR)
    # Simulate a stale/competing insert of a proven-safe row afterwards.
    store._conn.execute(
        "INSERT INTO attempts(run_id, principal, task_id, attempt_id, preset, "
        "driver_id, runner_instance, workspace_id, request_hash, status, outcome, "
        "synthetic, cached, created_at, updated_at) "
        "VALUES('run_x','alpha','task-1','att_x','mock/text','mock','runner-1',"
        "'ws-alpha','sha256:a','failed','rejected',1,0,'zz','zz')"
    )
    with pytest.raises(Conflict) as excinfo:
        _reserve(store, "run_2")
    assert excinfo.value.code == "run_not_retryable"
    store.close()


def test_reserve_blocks_failed_attempt_with_null_outcome(tmp_path):
    """A failed attempt whose outcome was never written (NULL) is a blocker.

    ``proven_pre_execution('failed', None, ...)`` is False, so the SQL mirror
    inside ``reserve()`` must agree. ``outcome IN (...)`` evaluates to NULL for
    a NULL outcome and ``NOT NULL`` is NULL — three-valued logic must never
    turn a missing outcome into an admission.
    """
    store = Store(str(tmp_path / "core.db"))
    store.initialize()
    _reserve(store, "run_1")
    store.set_attempt("run_1", status=FAILED)  # outcome stays NULL
    record = store.get_attempt("run_1")
    assert record.outcome is None
    assert (
        proven_pre_execution(record.status, record.outcome, record.started_at)
        is False
    )
    with pytest.raises(Conflict) as excinfo:
        _reserve(store, "run_2")
    assert excinfo.value.code == "run_not_retryable"
    store.close()


# Every persistable status, outcome shape and dispatch-marker state. Outcomes:
# None (never written), the two known pre-execution-safe values, known-unsafe
# values, and unknown/empty corrupt values.
_ALL_STATUSES = sorted(
    {
        RESERVED,
        QUEUED,
        STARTING,
        RUNNING,
        CANCELLING,
        COMPLETED,
        FAILED,
        CANCELLED,
        UNKNOWN,
        # A status outside the taxonomy is corrupt state; it must block too.
        "bogus-corrupt",
    }
)
_ALL_OUTCOMES = [
    None,
    OUTCOME_REJECTED,
    OUTCOME_QUEUE_TIMEOUT,
    OUTCOME_PROVIDER_ERROR,
    OUTCOME_SUCCEEDED,
    OUTCOME_CANCELLED,
    OUTCOME_UNKNOWN,
    "",
]
_STARTED_AT = [None, "2026-01-01T00:00:00+00:00"]
# The reference proven-pre-execution row used to build ancestor chains.
_SAFE_ROW = (FAILED, OUTCOME_REJECTED, None)


def _seed_attempt(
    store: Store,
    run_id: str,
    task: str,
    *,
    status: str,
    outcome: str | None,
    started_at: str | None,
) -> None:
    """Insert a history row directly, including states ``set_attempt`` cannot
    express (a NULL outcome on a terminal status, corrupt/empty outcomes)."""
    store._conn.execute(
        "INSERT INTO attempts(run_id, principal, task_id, attempt_id, preset, "
        "driver_id, runner_instance, workspace_id, request_hash, status, outcome, "
        "started_at, synthetic, cached, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,0,'2026-01-01T00:00:00+00:00',"
        "'2026-01-01T00:00:00+00:00')",
        (
            run_id,
            "alpha",
            task,
            f"att_{run_id}",
            "mock/text",
            "mock",
            "runner-1",
            "ws-alpha",
            "sha256:a",
            status,
            outcome,
            started_at,
        ),
    )


@pytest.mark.parametrize(
    "started_at", _STARTED_AT, ids=["no-dispatch-marker", "dispatched"]
)
@pytest.mark.parametrize(
    "outcome",
    _ALL_OUTCOMES,
    ids=[str(o) if o else ("null" if o is None else "empty") for o in _ALL_OUTCOMES],
)
@pytest.mark.parametrize("status", _ALL_STATUSES)
def test_reserve_admission_matches_python_contract(
    tmp_path, status, outcome, started_at
):
    """The SQL blocker predicate must agree with ``proven_pre_execution`` for
    every reachable or corrupt row state — solo, and as either ancestor in a
    two-attempt chain (a blocker anywhere in the history must still block)."""
    store = Store(str(tmp_path / "core.db"))
    store.initialize()
    expected = proven_pre_execution(status, outcome, started_at)
    expected_code = (
        "run_active" if status in LOCK_STATUSES else "run_not_retryable"
    )
    histories = {
        "solo": [(status, outcome, started_at)],
        "combo_then_safe": [(status, outcome, started_at), _SAFE_ROW],
        "safe_then_combo": [_SAFE_ROW, (status, outcome, started_at)],
    }
    for label, rows in histories.items():
        task = f"task-{label}"
        for index, (st, oc, sa) in enumerate(rows):
            _seed_attempt(
                store,
                f"{label}-{index}",
                task,
                status=st,
                outcome=oc,
                started_at=sa,
            )
        if expected:
            record = _reserve(store, f"new-{label}", task=task)
            assert record.status == QUEUED, label
        else:
            with pytest.raises(Conflict) as excinfo:
                _reserve(store, f"new-{label}", task=task)
            assert excinfo.value.code == expected_code, (
                label,
                excinfo.value.code,
            )
    store.close()
