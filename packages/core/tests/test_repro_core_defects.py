"""Deterministic regression tests for the reviewer's core findings.

These started as RED reproductions from the stopped worker; each expectation was
re-checked against the intended contract (finite deadline, no fabricated
effects) before being used as a GREEN guard.
"""

import asyncio

import pytest

from cli_provider_core.config import Concurrency
from cli_provider_core.controller import _SCHEDULING_MARGIN_SECONDS
from cli_provider_core.errors import RunnerRunRejected
from cli_provider_core.models import (
    CANCELLING,
    COMPLETED,
    FAILED,
    OUTCOME_REJECTED,
    OUTCOME_UNKNOWN,
    UNKNOWN,
)
from conftest import FakeControl, FakeSession
from test_controller import prepared, submit


def _runner(socket_path: str, **overrides) -> dict:
    runner = {
        "instance_id": "runner-1",
        "driver_id": "mock",
        "driver_version": "0.1.0",
        "distribution": "cli-driver-mock",
        "socket_path": socket_path,
    }
    runner.update(overrides)
    return runner


class _QueueFullSession(FakeSession):
    """Runner double that rejects the run before any execution effect."""

    async def run(self, params):
        raise RunnerRunRejected(
            "runner run error QUEUE_FULL",
            runner_code="QUEUE_FULL",
            retryable=True,
            stage="pre_execution",
        )
        yield  # pragma: no cover - makes this an async generator


class _MidStreamErrorSession(FakeSession):
    """Runner double that fails after events may already have had an effect."""

    async def run(self, params):
        yield self._event(
            params["run_id"], 1, "run.started", {"preset": params["preset"]}
        )
        raise RunnerRunRejected(
            "runner run error INTERNAL_ERROR",
            runner_code="INTERNAL_ERROR",
            stage="execution",
        )


def test_repro_issue_1_concurrency_default():
    """Finding 1: Concurrency.per_runner default must be 1, not 2."""
    assert Concurrency().per_runner == 1


@pytest.mark.asyncio
async def test_effective_concurrency_is_clamped_to_verified_runner_capacity(tmp_path):
    """Finding 1: dispatch is clamped to the Runner's declared capacity.

    Configured per_runner=4 against a runner that declares a single slot must
    never dispatch more than one run; the excess waits in the core's finite,
    observable queue instead of the Runner's opaque one.
    """
    config, store, registry, controller, control = await prepared(
        tmp_path,
        control=FakeControl(behavior="hang", max_parallel_runs=1),
        api={
            "concurrency": {
                "per_runner": 4,
                "per_principal": 4,
                "max_queued_per_runner": 4,
                "max_queued_per_principal": 4,
                "queue_timeout_seconds": 30.0,
            }
        },
    )
    assert registry.runner_capacity("runner-1") == 1
    first = await submit(controller, registry, task="clamp-1")
    await asyncio.sleep(0.05)
    await submit(controller, registry, task="clamp-2")
    await asyncio.sleep(0.2)
    assert control.executions == 1
    await controller.cancel(first.record.run_id, "alpha")
    store.close()


@pytest.mark.asyncio
async def test_repro_issue_2_run_frame_timeout_override_tightens(tmp_path):
    """Finding 2: an explicit operator override is honored, not overwritten."""
    config, store, registry, controller, control = await prepared(
        tmp_path,
        runners=[_runner(str(tmp_path / "mock.sock"), run_frame_timeout_seconds=3.0)],
    )
    sub = await submit(controller, registry)  # deadline 5.0, cancel 1.0 -> budget 11.0
    await sub.active.task
    assert control.run_timeouts == [3.0]
    store.close()


@pytest.mark.asyncio
async def test_repro_issue_2_override_never_exceeds_finite_budget(tmp_path):
    """Finding 2: the override cannot exceed the finite deadline contract."""
    config, store, registry, controller, control = await prepared(
        tmp_path,
        runners=[_runner(str(tmp_path / "mock.sock"), run_frame_timeout_seconds=42.0)],
    )
    sub = await submit(controller, registry)
    await sub.active.task
    budget = controller.run_budget("runner-1", 5.0)
    assert control.run_timeouts == [budget]
    store.close()


@pytest.mark.asyncio
async def test_outer_budget_derives_from_runner_cleanup_not_api_config(tmp_path):
    """Finding 5: the finite outer budget follows the Runner's declared cleanup.

    The API's own cancel deadline is deliberately mismatched (smaller) than the
    Runner's cleanup budget; the derived bound must still cover the Runner's
    worst-case unwind instead of cutting it short.
    """
    config, store, registry, controller, control = await prepared(
        tmp_path,
        control=FakeControl(cancel_cleanup_seconds=9.0),
        api={"cancel_deadline_seconds": 0.5},
        runners=[_runner(str(tmp_path / "mock.sock"), run_frame_timeout_seconds=42.0)],
    )
    expected = 5.0 + 9.0 + _SCHEDULING_MARGIN_SECONDS
    assert config.api.cancel_deadline_seconds == 0.5
    assert registry.runner_cleanup_seconds("runner-1") == 9.0
    assert controller.run_budget("runner-1", 5.0) == expected
    sub = await submit(controller, registry)
    await sub.active.task
    assert control.run_timeouts == [expected]
    store.close()


@pytest.mark.asyncio
async def test_repro_issue_5_runner_pre_execution_rejection_no_quarantine(tmp_path):
    """Finding 5: a proven pre-execution rejection is FAILED/rejected, no quarantine."""
    config, store, registry, controller, control = await prepared(tmp_path)
    registry._session_factory = lambda cfg: _QueueFullSession(cfg, control)

    sub = await submit(controller, registry)
    record = await sub.active.task
    assert store.get_quarantine("runner-1") is None, (
        "A provably pre-execution rejection must not quarantine the Runner"
    )
    assert record.status == FAILED
    assert record.outcome == OUTCOME_REJECTED
    store.close()


@pytest.mark.asyncio
async def test_runner_rejection_after_execution_still_quarantines(tmp_path):
    """The fail-closed side: an error after events may have had an effect."""
    config, store, registry, controller, control = await prepared(tmp_path)
    registry._session_factory = lambda cfg: _MidStreamErrorSession(cfg, control)

    sub = await submit(controller, registry)
    record = await sub.active.task
    assert record.status == UNKNOWN
    assert record.outcome == OUTCOME_UNKNOWN
    assert store.get_quarantine("runner-1") is not None
    store.close()


@pytest.mark.asyncio
async def test_repro_issue_6_cancelling_never_overwritten_by_running(tmp_path):
    """Finding 6: the first event must never regress CANCELLING back to RUNNING."""
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "hang"
    sub = await submit(controller, registry)
    run_id = sub.record.run_id

    store.set_attempt(run_id, status=CANCELLING)
    for _ in range(100):
        if store.count_events(run_id) >= 1:
            break
        await asyncio.sleep(0.01)

    assert store.get_attempt(run_id).status == CANCELLING

    control.hang_event.set()
    await sub.active.task
    store.close()


@pytest.mark.asyncio
async def test_repro_issue_6_reconcile_quarantine_after_uncertain_cancel(tmp_path):
    """Finding 6: a validated terminal after an uncertain cancel reconciles quarantine."""
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "release_completed"

    sub = await submit(controller, registry)
    run_id = sub.record.run_id
    await asyncio.sleep(0.05)

    control.cancel_confirmed = False
    controller._config.api.cancel_deadline_seconds = 0.05

    cancel_view = await controller.cancel(run_id, "alpha")
    assert cancel_view.status == UNKNOWN
    assert store.get_quarantine("runner-1") is not None

    control.hang_event.set()
    record = await sub.active.task
    assert record.status == COMPLETED
    assert store.get_quarantine("runner-1") is None, (
        "A validated terminal must reconcile the matching quarantine"
    )
    store.close()
