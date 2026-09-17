import asyncio

import pytest

from cli_driver_mock import MockBehavior, MockDriver, MockMalformedMode
from cli_provider_sdk import (
    EventKind,
    Message,
    NormalizedRequest,
    RuntimeContext,
    SDK_VERSION,
    StructuredOutputMode,
    TransportKind,
    WorkspaceRef,
    is_terminal_kind,
)


def request() -> NormalizedRequest:
    return NormalizedRequest(
        run_id="run-1",
        task_id="task-1",
        attempt_id="att-1",
        preset="mock/text",
        workspace=WorkspaceRef(workspace_id="ws-1"),
        messages=[Message(role="user", content="say hi")],
    )


async def collect(driver: MockDriver) -> list:
    events = []
    async for event in driver.execute(request(), RuntimeContext()):
        events.append(event)
    return events


def test_manifest_and_capabilities_are_conservative():
    driver = MockDriver(behavior=MockBehavior.SUCCESS)
    manifest = driver.manifest
    assert manifest.driver_id == "mock"
    assert manifest.synthetic is True
    assert manifest.sdk_version == SDK_VERSION
    assert manifest.supported_transports == [TransportKind.STDIO]

    caps = asyncio.run(driver.probe(RuntimeContext())).capabilities
    assert caps.external_tool_calls is False
    assert caps.structured_output is StructuredOutputMode.NONE
    assert caps.vision is False
    assert caps.workspace_write is False
    assert caps.web_search is False


def test_success_uses_canonical_event_names_and_sequence():
    events = asyncio.run(collect(MockDriver(behavior=MockBehavior.SUCCESS)))
    assert events[0].kind == EventKind.RUN_STARTED
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert all(e.schema_version == 1 for e in events)
    terminals = [e for e in events if is_terminal_kind(e.kind)]
    assert len(terminals) == 1
    assert terminals[0].kind == EventKind.RUN_COMPLETED
    assert events[-1].kind == EventKind.RUN_COMPLETED
    assert all(e.synthetic for e in events)
    assert [e.run_id for e in events] == ["run-1"] * len(events)
    assert any(e.kind == EventKind.MESSAGE_DELTA for e in events)


def test_partial_mode_reports_partial_completion():
    events = asyncio.run(collect(MockDriver(behavior=MockBehavior.PARTIAL)))
    assert events[-1].kind == EventKind.RUN_COMPLETED
    assert events[-1].payload.outcome == "partial"


def test_failed_mode_emits_explicit_failed_terminal():
    events = asyncio.run(collect(MockDriver(behavior=MockBehavior.FAILED)))
    assert events[-1].kind == EventKind.RUN_FAILED
    assert events[-1].payload.code


def test_crash_mode_raises_abruptly():
    async def run() -> None:
        async for _ in MockDriver(behavior=MockBehavior.CRASH).execute(
            request(), RuntimeContext()
        ):
            pass

    with pytest.raises(RuntimeError):
        asyncio.run(run())


def test_hang_mode_is_cancellable_and_yields_cancelled_terminal():
    driver = MockDriver(behavior=MockBehavior.HANG)

    async def run() -> list:
        events = []
        async for event in driver.execute(request(), RuntimeContext()):
            events.append(event)
        return events

    async def scenario() -> list:
        task = asyncio.create_task(run())
        await asyncio.sleep(0.05)
        result = await driver.cancel("run-1", RuntimeContext())
        assert result.confirmed is True
        return await asyncio.wait_for(task, timeout=2)

    events = asyncio.run(scenario())
    assert events[-1].kind == EventKind.RUN_CANCELLED


def test_hang_ignores_cancel_mode_never_terminates():
    driver = MockDriver(behavior=MockBehavior.HANG_IGNORES_CANCEL)

    async def run() -> None:
        async for _ in driver.execute(request(), RuntimeContext()):
            pass

    async def scenario() -> None:
        task = asyncio.create_task(run())
        await asyncio.sleep(0.05)
        result = await driver.cancel("run-1", RuntimeContext())
        assert result.confirmed is False
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=0.2)
        task.cancel()

    asyncio.run(scenario())


def test_slow_mode_streams_over_time():
    async def scenario() -> list:
        driver = MockDriver(behavior=MockBehavior.SLOW)
        seen = []
        async for event in driver.execute(request(), RuntimeContext()):
            seen.append(event)
            if len(seen) >= 3:
                break
        return seen

    seen = asyncio.run(scenario())
    assert seen[0].kind == EventKind.RUN_STARTED
    assert seen[1].kind == EventKind.MESSAGE_DELTA


def test_malformed_duplicate_sequence():
    events = asyncio.run(
        collect(
            MockDriver(
                behavior=MockBehavior.MALFORMED,
                malformed_mode=MockMalformedMode.DUPLICATE_SEQUENCE,
            )
        )
    )
    assert [e.sequence for e in events].count(events[-1].sequence) >= 2


def test_malformed_event_after_terminal():
    events = asyncio.run(
        collect(
            MockDriver(
                behavior=MockBehavior.MALFORMED,
                malformed_mode=MockMalformedMode.EVENT_AFTER_TERMINAL,
            )
        )
    )
    assert events[0].kind == EventKind.RUN_COMPLETED
    assert len(events) > 1


def test_malformed_no_terminal():
    events = asyncio.run(
        collect(
            MockDriver(
                behavior=MockBehavior.MALFORMED,
                malformed_mode=MockMalformedMode.NO_TERMINAL,
            )
        )
    )
    assert events
    assert not any(is_terminal_kind(e.kind) for e in events)


def test_behavior_comes_from_operator_environment_not_request(monkeypatch):
    monkeypatch.setenv("CLI_DRIVER_MOCK_BEHAVIOR", "failed")
    assert MockDriver().behavior is MockBehavior.FAILED


def test_discover_models_is_synthetic_and_unverified():
    models = asyncio.run(MockDriver().discover_models(RuntimeContext()))
    assert models
    assert models[0].verification.status.value != "passed"
