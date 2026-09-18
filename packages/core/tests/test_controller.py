import asyncio

import pytest

from cli_provider_core import (
    ActiveRun,
    Conflict,
    QueueFull,
    QueueTimeout,
    RunnerQuarantined,
    hash_api_key,
)

from conftest import FakeControl, make_system

MESSAGES = [{"role": "user", "content": "hello"}]


async def submit(
    controller, registry, *, task="task-1", preset="mock/text", messages=None,
    principal="alpha", principal_concurrency=2,
):
    return await controller.submit(
        principal=principal,
        principal_concurrency=principal_concurrency,
        task_id=task,
        preset=registry.preset_config(preset),
        workspace_id="ws-alpha",
        messages=messages or MESSAGES,
        deadline_seconds=5.0,
    )


async def prepared(tmp_path, **overrides):
    config, store, registry, controller, control = make_system(tmp_path, **overrides)
    await registry.refresh()
    return config, store, registry, controller, control


async def test_success_run_completes_with_summary_and_artifact(tmp_path):
    config, store, registry, controller, control = await prepared(tmp_path)
    sub = await submit(controller, registry)
    record = await sub.active.task
    assert record.status == "completed"
    assert record.outcome == "succeeded"
    assert record.summary == "hello world"
    assert record.verification["status"] == "not_run"
    assert record.usage["provenance"] == "unknown"
    artifacts = store.list_artifacts(record.run_id)
    assert len(artifacts) == 1
    assert open(artifacts[0].path, encoding="utf-8").read() == "hello world"
    assert store.count_events(record.run_id) == 4
    store.close()


async def test_partial_completion_is_preserved(tmp_path):
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "partial"
    sub = await submit(controller, registry)
    record = await sub.active.task
    assert record.status == "completed"
    assert record.outcome == "partial"
    store.close()


async def test_failed_run_is_failed_not_success(tmp_path):
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "failed"
    sub = await submit(controller, registry)
    record = await sub.active.task
    assert record.status == "failed"
    assert record.outcome == "provider_error"
    store.close()


async def test_unknown_run_quarantines_runner(tmp_path):
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "crash"
    sub = await submit(controller, registry)
    record = await sub.active.task
    assert record.status == "unknown"
    assert store.get_quarantine("runner-1") is not None
    store.close()


async def test_quarantine_blocks_a_different_task(tmp_path):
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "crash"
    sub = await submit(controller, registry, task="task-1")
    await sub.active.task
    with pytest.raises(RunnerQuarantined):
        await submit(controller, registry, task="task-2")
    store.close()


async def test_cached_replay_does_not_execute_again(tmp_path):
    config, store, registry, controller, control = await prepared(tmp_path)
    first = await submit(controller, registry)
    record = await first.active.task
    second = await submit(controller, registry)
    assert second.cached is True
    assert second.record.run_id == record.run_id
    assert second.record.cached is True
    assert control.executions == 1
    store.close()


async def test_changed_body_under_same_task_conflicts(tmp_path):
    config, store, registry, controller, control = await prepared(tmp_path)
    first = await submit(controller, registry)
    await first.active.task
    with pytest.raises(Conflict) as excinfo:
        await submit(
            controller, registry, messages=[{"role": "user", "content": "different"}]
        )
    assert excinfo.value.code == "task_content_conflict"
    store.close()


async def test_different_model_does_not_relabel_completed_result(tmp_path):
    # Same operator task policy (text), different preset alias: the logical
    # request hash matches, so this is a model relabel conflict, not a content
    # conflict.
    config, store, registry, controller, control = await prepared(tmp_path)
    first = await submit(controller, registry, preset="mock/text")
    await first.active.task
    with pytest.raises(Conflict) as excinfo:
        await submit(controller, registry, preset="mock/text-beta")
    assert excinfo.value.code == "model_conflict"
    store.close()


async def test_a_different_preset_task_policy_is_a_content_conflict(tmp_path):
    # mock/review carries a different operator task policy, so the derived
    # request hash must differ rather than silently sharing the text-policy hash.
    config, store, registry, controller, control = await prepared(tmp_path)
    first = await submit(controller, registry, preset="mock/text")
    await first.active.task
    with pytest.raises(Conflict) as excinfo:
        await submit(controller, registry, preset="mock/review")
    assert excinfo.value.code == "task_content_conflict"
    store.close()


async def test_run_frame_timeout_follows_the_deadline_budget(tmp_path):
    # The streaming frame timeout is derived from the run's deadline + the
    # Runner's declared cleanup budget, never a fixed 15 s.
    config, store, registry, controller, control = await prepared(tmp_path)
    sub = await submit(controller, registry)
    await sub.active.task
    expected = controller.run_budget("runner-1", 5.0)
    assert control.run_timeouts == [expected]
    store.close()


async def test_nonstream_many_events_completes_without_a_subscriber(tmp_path):
    # Regression: 300 events with stream:false and no SSE subscriber. The fan-out
    # must never block the run, and a healthy run must not become unknown.
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "many_events"
    control.event_count = 300
    sub = await submit(controller, registry, preset="mock/text")
    record = await asyncio.wait_for(sub.active.task, timeout=10)
    assert record.status == "completed"
    assert record.outcome == "succeeded"
    assert store.count_events(record.run_id) == 302
    assert store.get_quarantine("runner-1") is None
    assert len(store.list_artifacts(record.run_id)) == 1
    store.close()


async def test_late_cancel_does_not_downgrade_a_validated_completion(tmp_path):
    # Race: the run completes while cancel() is awaiting; the validated
    # completed terminal (and its artifact) must be preserved, not quarantined.
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "release_completed"
    sub = await submit(controller, registry)
    await asyncio.sleep(0.05)
    view = await controller.cancel(sub.record.run_id, "alpha")
    assert view.requested is True
    assert view.confirmed is False
    record = store.get_attempt(sub.record.run_id)
    assert record.status == "completed"
    assert record.outcome == "succeeded"
    assert record.summary == "finished anyway"
    assert store.get_quarantine("runner-1") is None
    assert len(store.list_artifacts(record.run_id)) == 1
    store.close()


async def test_queued_cancel_racing_into_start_escalates_to_driver_cancel(tmp_path):
    # Race: cancel reads `queued`, but dispatch has already begun, so the
    # pre-start flag is no longer watched. The cancel must be escalated to a real
    # driver cancel rather than dropped and quarantined.
    config, store, registry, controller, control = await prepared(tmp_path)
    record = store.reserve(
        run_id="run_race",
        attempt_id="att_race",
        principal="alpha",
        task_id="task-race",
        preset="mock/text",
        driver_id="mock",
        runner_instance="runner-1",
        workspace_id="ws-alpha",
        request_hash="sha256:race",
        task_policy="text",
        status="queued",
    )
    release = control.hang_event

    async def fake_execute() -> None:
        await asyncio.sleep(0.1)
        store.set_attempt("run_race", status="starting")
        await release.wait()
        controller._finish("run_race", status="cancelled", outcome="cancelled")

    active = ActiveRun(record=record, messages=MESSAGES)
    active.task = asyncio.create_task(fake_execute())
    controller._active["run_race"] = active

    view = await controller.cancel("run_race", "alpha")
    assert view.requested is True
    assert view.confirmed is True
    assert control.cancel_calls == 1
    assert store.get_attempt("run_race").status == "cancelled"
    assert store.get_quarantine("runner-1") is None
    store.close()


async def test_runner_queue_bound_refuses_excess_without_runner_effect(tmp_path):
    config, store, registry, controller, control = await prepared(
        tmp_path,
        api={
            "concurrency": {
                "per_runner": 1,
                "per_principal": 2,
                "max_queued_per_runner": 1,
                "queue_timeout_seconds": 30.0,
            }
        },
    )
    control.behavior = "hang"
    first = await submit(controller, registry, task="task-1")
    await asyncio.sleep(0.05)
    second = await submit(controller, registry, task="task-2")
    await asyncio.sleep(0.05)

    with pytest.raises(QueueFull) as excinfo:
        await submit(controller, registry, task="task-3")
    assert excinfo.value.http_status == 429
    # No Runner effect: the excess task was never allocated.
    assert control.executions == 1
    assert store.get_task("alpha", "task-3") is None
    assert store.latest_attempt("alpha", "task-3") is None

    # Capacity recovers when a queued run is cancelled.
    await controller.cancel(second.record.run_id, "alpha")
    third = await submit(controller, registry, task="task-3")
    assert third.cached is False
    await controller.cancel(first.record.run_id, "alpha")
    await controller.cancel(third.record.run_id, "alpha")
    store.close()


async def test_per_principal_queue_bounds_are_independent(tmp_path):
    principals = [
        {
            "name": "alpha",
            "key_hash": hash_api_key("secret-alpha"),
            "allowed_presets": ["mock/text"],
            "allowed_workspaces": ["ws-alpha"],
            "max_concurrency": 1,
        },
        {
            "name": "beta",
            "key_hash": hash_api_key("secret-beta"),
            "allowed_presets": ["mock/text"],
            "allowed_workspaces": ["ws-alpha"],
            "max_concurrency": 1,
        },
    ]
    config, store, registry, controller, control = make_system(
        tmp_path,
        control=FakeControl(behavior="hang"),
        principals=principals,
        api={
            "concurrency": {
                "per_runner": 5,
                "per_principal": 1,
                "max_queued_per_principal": 1,
                "queue_timeout_seconds": 30.0,
            }
        },
    )
    await registry.refresh()

    a1 = await submit(controller, registry, task="a-1", principal="alpha", principal_concurrency=1)
    a2 = await submit(controller, registry, task="a-2", principal="alpha", principal_concurrency=1)
    with pytest.raises(QueueFull):
        await submit(controller, registry, task="a-3", principal="alpha", principal_concurrency=1)
    # A saturated principal does not consume another principal's capacity.
    b1 = await submit(controller, registry, task="b-1", principal="beta", principal_concurrency=1)
    b2 = await submit(controller, registry, task="b-2", principal="beta", principal_concurrency=1)
    with pytest.raises(QueueFull):
        await submit(controller, registry, task="b-3", principal="beta", principal_concurrency=1)

    for sub in (a1, a2, b1, b2):
        await controller.cancel(sub.record.run_id, sub.record.principal)
    store.close()


async def test_unknown_is_never_rerun(tmp_path):
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "unknown"
    first = await submit(controller, registry)
    record = await first.active.task
    assert record.status == "unknown"
    with pytest.raises(Conflict) as excinfo:
        await submit(controller, registry)
    assert excinfo.value.code == "unknown_attempt"
    assert control.executions == 1
    store.close()


async def test_cancel_queued_never_starts_driver(tmp_path):
    config, store, registry, controller, control = await prepared(
        tmp_path, api={"concurrency": {"per_runner": 1, "per_principal": 1}}
    )
    control.behavior = "hang"
    first = await submit(controller, registry, task="task-1")
    await asyncio.sleep(0.05)
    second = await submit(controller, registry, task="task-2")
    await asyncio.sleep(0.05)
    view = await controller.cancel(second.record.run_id, "alpha")
    assert view.requested is True
    assert view.confirmed is True
    assert control.executions == 1  # only the first run started
    second_record = store.get_attempt(second.record.run_id)
    assert second_record.status == "cancelled"
    assert store.count_events(second.record.run_id) == 0
    view1 = await controller.cancel(first.record.run_id, "alpha")
    assert view1.confirmed is True
    store.close()


async def test_cancel_running_reports_requested_and_confirmed(tmp_path):
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "hang"
    sub = await submit(controller, registry)
    await asyncio.sleep(0.05)
    view = await controller.cancel(sub.record.run_id, "alpha")
    assert view.requested is True
    assert view.confirmed is True
    assert control.cancel_calls == 1
    assert store.get_attempt(sub.record.run_id).status == "cancelled"
    store.close()


async def test_unconfirmed_cancel_becomes_unknown_and_quarantines(tmp_path):
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "hang"
    control.cancel_confirmed = False
    sub = await submit(controller, registry)
    await asyncio.sleep(0.05)
    view = await controller.cancel(sub.record.run_id, "alpha")
    assert view.confirmed is False
    assert store.get_attempt(sub.record.run_id).status == "unknown"
    assert store.get_quarantine("runner-1") is not None
    sub.active.task.cancel()
    store.close()


async def test_queue_timeout_is_reported(tmp_path):
    config, store, registry, controller, control = await prepared(
        tmp_path,
        api={
            "concurrency": {
                "per_runner": 1,
                "per_principal": 1,
                "queue_timeout_seconds": 0.2,
            }
        },
    )
    control.behavior = "hang"
    first = await submit(controller, registry, task="task-1")
    await asyncio.sleep(0.05)
    second = await submit(controller, registry, task="task-2")
    record = await second.active.task
    assert record.status == "failed"
    assert record.outcome == "queue_timeout"
    await controller.cancel(first.record.run_id, "alpha")
    store.close()


async def test_ownership_is_enforced_on_reads_and_cancel(tmp_path):
    from cli_provider_core import NotFound

    config, store, registry, controller, control = await prepared(tmp_path)
    sub = await submit(controller, registry)
    await sub.active.task
    with pytest.raises(NotFound):
        controller.owned_attempt(sub.record.run_id, "someone-else")
    with pytest.raises(NotFound):
        controller.owned_attempt("run_missing", "alpha")
    with pytest.raises(NotFound):
        await controller.cancel(sub.record.run_id, "someone-else")
    store.close()
