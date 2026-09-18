"""Antigravity driver tests. Synthetic fixtures only - no real CLI or account."""

from __future__ import annotations

import asyncio
import gc
import signal
import time
import importlib.metadata
import json
import os
import sys
from pathlib import Path

import pytest

from cli_provider_sdk import (
    EventKind,
    RoleMode,
    SessionMode,
    StreamingMode,
    StructuredOutputMode,
    UsageProvenance,
    Message,
    NormalizedRequest,
    ProviderDriver,
    RuntimeContext,
    WorkspaceRef,
)
from cli_provider_transports import LocalProcessExecutor

from cli_driver_antigravity import AntigravityDriver

FIXTURE = Path(__file__).parent / "fixtures" / "fake_agy.py"
ENTRY_POINT_GROUP = "cli_provider.drivers"


@pytest.fixture(autouse=True)
async def _collect_subprocess_transports():
    """Collect any lingering subprocess transport while the loop is still open.

    asyncio defers pipe cleanup with call_soon; a transport collected after the
    test's loop closed would raise from ``__del__`` and hide real failures.
    """
    yield
    gc.collect()


def make_wrapper(tmp_path: Path) -> Path:
    """A real executable whose argv is the fixture script plus the CLI flags."""
    wrapper = tmp_path / "agy"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def make_ctx(
    tmp_path: Path,
    mode: str,
    *,
    pidfile: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> RuntimeContext:
    env = dict(os.environ)
    env["FAKE_AGY_MODE"] = mode
    if pidfile is not None:
        env["FAKE_AGY_PIDFILE"] = str(pidfile)
    env.update(extra_env or {})
    return RuntimeContext(executor=LocalProcessExecutor(env=env))


def make_request(
    *,
    run_id: str = "run_agy_1",
    deadline: float | None = 10.0,
    preset: str = "agy/review",
    model_alias: str | None = None,
) -> NormalizedRequest:
    return NormalizedRequest(
        run_id=run_id,
        task_id="task_agy_1",
        attempt_id="attempt_agy_1",
        preset=preset,
        workspace=WorkspaceRef(workspace_id="ws-alpha"),
        model_alias=model_alias,
        messages=[Message(role="user", content="summarize the module")],
        deadline_seconds=deadline,
    )


async def collect(driver: AntigravityDriver, request: NormalizedRequest, ctx: RuntimeContext) -> list:
    return [event async for event in driver.execute(request, ctx)]


def kinds(events: list) -> list[str]:
    return [event.kind for event in events]


def answer_text(events: list) -> str:
    return "".join(
        event.payload.text for event in events if event.kind == EventKind.MESSAGE_DELTA
    )


def terminal(events: list):
    terminals = [event for event in events if event.kind in (
        EventKind.RUN_COMPLETED,
        EventKind.RUN_FAILED,
        EventKind.RUN_CANCELLED,
    )]
    assert len(terminals) == 1, f"expected exactly one terminal event, got {kinds(events)}"
    return terminals[0]


# ----------------------------------------------------------------- packaging


def test_entry_point_is_registered_for_the_runner_allowlist():
    entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    matches = [entry for entry in entry_points if entry.name == "antigravity"]
    assert matches, "antigravity driver is not registered in cli_provider.drivers"
    entry = matches[0]
    assert entry.value == "cli_driver_antigravity:AntigravityDriver"
    assert entry.dist is not None
    assert entry.dist.metadata["Name"].replace("_", "-").lower() == "cli-driver-antigravity"
    assert entry.dist.version == "0.1.0"


def test_driver_satisfies_the_sdk_protocol_and_conservative_capabilities():
    driver = AntigravityDriver()
    assert isinstance(driver, ProviderDriver)
    manifest = driver.manifest
    assert manifest.driver_id == "antigravity"
    assert manifest.synthetic is False
    assert manifest.sdk_version
    assert manifest.supported_transports
    capabilities = driver.capabilities()
    # Unverified abilities stay false rather than being assumed.
    assert capabilities.workspace_write is False
    assert capabilities.vision is False
    assert capabilities.web_search is False
    assert capabilities.external_tool_calls is False
    assert capabilities.internal_tools is True
    # Structured output is not implemented, so it must not be advertised; usage
    # and session/role modes are pinned here so an overstatement cannot return.
    assert capabilities.structured_output is StructuredOutputMode.NONE
    assert capabilities.usage is UsageProvenance.UNKNOWN
    assert capabilities.sessions is SessionMode.NONE
    assert capabilities.roles is RoleMode.SERIALIZED
    assert capabilities.streaming is StreamingMode.NATIVE


# --------------------------------------------------------------------- probe


async def test_probe_reports_the_exact_cli_version(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    report = await driver.probe(make_ctx(tmp_path, "ok"))
    assert report.ok is True
    assert report.cli_version == "1.2.5"
    assert report.capabilities.internal_tools is True


async def test_probe_fails_closed_without_an_executor():
    report = await AntigravityDriver().probe(RuntimeContext())
    assert report.ok is False
    assert any("no process executor" in note for note in report.notes)


async def test_probe_fails_closed_when_the_version_cannot_be_parsed(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    report = await driver.probe(make_ctx(tmp_path, "bad_version"))
    assert report.ok is False
    assert report.cli_version is None


async def test_probe_refuses_a_version_that_does_not_match_the_pin(tmp_path):
    driver = AntigravityDriver(
        cli_command=str(make_wrapper(tmp_path)), expected_version="9.9.9"
    )
    report = await driver.probe(make_ctx(tmp_path, "ok"))
    assert report.ok is False
    assert report.cli_version == "1.2.5"


# ----------------------------------------------------------------- discovery


async def test_discovery_requires_operator_pins_and_never_claims_verification(tmp_path):
    unpinned = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    assert await unpinned.discover_models(make_ctx(tmp_path, "ok")) == []

    pinned = AntigravityDriver(
        cli_command=str(make_wrapper(tmp_path)), models=["claude-opus-4-6-thinking"]
    )
    models = await pinned.discover_models(make_ctx(tmp_path, "ok"))
    assert [model.model_id for model in models] == ["claude-opus-4-6-thinking"]
    assert models[0].verification.status.value == "unknown"


# ------------------------------------------------------------------- execute


async def test_only_agent_response_text_becomes_answer_text(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "ok"))
    assert answer_text(events) == "hello world"
    assert "SECRET-PLANNING-TEXT" not in answer_text(events)
    assert kinds(events).count(EventKind.TOOL_STARTED) == 1
    assert kinds(events).count(EventKind.TOOL_COMPLETED) == 1
    result = terminal(events)
    assert result.kind == EventKind.RUN_COMPLETED
    assert result.payload.outcome == "succeeded"
    assert result.payload.usage.provenance.value == "unknown"


async def test_planning_step_text_delta_is_not_answer_text(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "planning_delta"))
    assert answer_text(events) == ""
    assert terminal(events).kind == EventKind.RUN_COMPLETED


async def test_planning_only_run_produces_no_answer_delta(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "planning_only"))
    assert answer_text(events) == ""
    assert terminal(events).kind == EventKind.RUN_COMPLETED


async def test_stderr_never_becomes_answer_text_or_events(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "stderr_noise"))
    assert answer_text(events) == "hello "
    # Serialize the WHOLE event stream (payloads included), not just the kinds:
    # asserting on kinds alone could never fail.
    serialized = json.dumps(
        [event.model_dump(mode="json") for event in events], ensure_ascii=False
    )
    assert "SECRET-STDERR-TOKEN" not in serialized


async def test_malformed_frame_is_a_protocol_error_not_success(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "malformed"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "protocol_error"


async def test_oversize_frame_is_rejected(tmp_path):
    driver = AntigravityDriver(
        cli_command=str(make_wrapper(tmp_path)), max_frame_bytes=4096
    )
    events = await collect(driver, make_request(), make_ctx(tmp_path, "oversize"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "protocol_error"


async def test_eof_without_a_result_frame_is_never_success(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "no_result"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "missing_result"


async def test_frames_before_init_are_refused(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "no_init"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "protocol_error"


async def test_deeply_nested_frame_is_a_bounded_protocol_error(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "deeply_nested"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "protocol_error"


async def test_undocumented_event_is_refused(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "unknown_event"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "unknown_event"


async def test_denied_tool_keeps_the_run_partial_not_verified(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "denied"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_COMPLETED
    assert result.payload.outcome == "partial"


async def test_cli_reported_error_is_a_failed_run(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "error_result"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "cli_reported_error"


async def test_no_deadline_and_no_executor_fail_closed(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)))
    no_deadline = await collect(driver, make_request(deadline=None), make_ctx(tmp_path, "ok"))
    assert terminal(no_deadline).payload.code == "no_deadline"
    no_executor = await collect(driver, make_request(), RuntimeContext())
    assert terminal(no_executor).payload.code == "no_process_executor"


async def test_public_alias_is_never_passed_to_the_cli(tmp_path):
    seen: list[list[str]] = []

    class RecordingExecutor(LocalProcessExecutor):
        async def spawn(self, argv, *, cwd=None):
            seen.append(list(argv))
            return await super().spawn(argv, cwd=cwd)

    env = dict(os.environ)
    env["FAKE_AGY_MODE"] = "ok"
    driver = AntigravityDriver(
        cli_command=str(make_wrapper(tmp_path)), model="claude-opus-4-6-thinking"
    )
    ctx = RuntimeContext(executor=RecordingExecutor(env=env))
    await collect(driver, make_request(preset="agy/review"), ctx)
    assert seen, "the CLI was never spawned"
    argv = seen[-1]
    assert "--input-format" in argv and "--output-format" in argv
    assert "-p" not in argv
    assert "agy/review" not in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-4-6-thinking"


async def test_deadline_terminates_the_cli_without_claiming_success(tmp_path):
    driver = AntigravityDriver(
        cli_command=str(make_wrapper(tmp_path)), grace_seconds=0.5
    )
    events = await collect(driver, make_request(deadline=0.6), make_ctx(tmp_path, "hang"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_CANCELLED
    assert "deadline" in result.payload.reason


async def test_cancel_of_a_responsive_cli_is_cancelled_not_failed(tmp_path):
    # A responsive CLI exits when asked to stop, which closes its stdout. That
    # EOF must not be relabelled as a provider failure.
    driver = AntigravityDriver(
        cli_command=str(make_wrapper(tmp_path)), grace_seconds=0.5
    )
    request = make_request(deadline=30.0)
    ctx = make_ctx(tmp_path, "hang")
    events: list = []

    async def consume() -> None:
        async for event in driver.execute(request, ctx):
            events.append(event)

    task = asyncio.ensure_future(consume())
    for _ in range(100):
        if events:
            break
        await asyncio.sleep(0.05)
    assert events, "the run never started"

    result = await driver.cancel(request.run_id, ctx)
    assert result.requested is True
    assert result.confirmed is True
    await asyncio.wait_for(task, timeout=15)
    assert terminal(events).kind == EventKind.RUN_CANCELLED


async def test_kill_that_truncates_a_frame_is_cancelled_not_a_protocol_error(tmp_path):
    driver = AntigravityDriver(
        cli_command=str(make_wrapper(tmp_path)), grace_seconds=0.5
    )
    request = make_request(deadline=30.0)
    ctx = make_ctx(tmp_path, "partial_frame")
    events: list = []

    async def consume() -> None:
        async for event in driver.execute(request, ctx):
            events.append(event)

    task = asyncio.ensure_future(consume())
    for _ in range(100):
        if events:
            break
        await asyncio.sleep(0.05)
    assert events, "the run never started"

    result = await driver.cancel(request.run_id, ctx)
    assert result.confirmed is True
    await asyncio.wait_for(task, timeout=15)
    # Our own kill leaves an unterminated line; that must not be reported as a
    # provider protocol error.
    terminal_event = terminal(events)
    assert terminal_event.kind == EventKind.RUN_CANCELLED


async def test_surviving_descendant_is_reported_not_claimed_killed(tmp_path):
    # The leader exits promptly on SIGTERM but leaves a descendant that ignores
    # it. Chasing the group after the leader is reaped is unsafe (the pgid can be
    # reused), so the documented behaviour is to REPORT the survivor.
    pidfile = tmp_path / "survivor.pid"
    driver = AntigravityDriver(
        cli_command=str(make_wrapper(tmp_path)), grace_seconds=0.5
    )
    request = make_request(deadline=30.0)
    ctx = make_ctx(
        tmp_path,
        "leader_exits_descendant_ignores",
        pidfile=pidfile,
        extra_env={"FAKE_AGY_READYFILE": str(tmp_path / "survivor.ready")},
    )
    events: list = []

    async def consume() -> None:
        async for event in driver.execute(request, ctx):
            events.append(event)

    task = asyncio.ensure_future(consume())
    for _ in range(100):
        if pidfile.exists() and events:
            break
        await asyncio.sleep(0.05)
    assert pidfile.exists(), "fixture never reported its descendant pid"
    survivor = int(pidfile.read_text().strip())

    try:
        result = await driver.cancel(request.run_id, ctx)
        assert result.confirmed is True
        assert "still alive" in (result.detail or ""), result.detail
        await asyncio.wait_for(task, timeout=15)
        assert terminal(events).kind == EventKind.RUN_CANCELLED
        # The survivor is still running because it was deliberately not chased.
        os.kill(survivor, 0)
    finally:
        try:
            os.kill(survivor, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_cancel_confirms_group_termination_including_descendants(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    driver = AntigravityDriver(
        cli_command=str(make_wrapper(tmp_path)), grace_seconds=0.5
    )
    request = make_request(deadline=30.0)
    ctx = make_ctx(
        tmp_path,
        "ignore_term",
        pidfile=pidfile,
        extra_env={"FAKE_AGY_READYFILE": str(tmp_path / "ignoring.ready")},
    )
    events: list = []

    async def consume() -> None:
        async for event in driver.execute(request, ctx):
            events.append(event)

    task = asyncio.ensure_future(consume())
    for _ in range(100):
        if pidfile.exists():
            break
        await asyncio.sleep(0.05)
    assert pidfile.exists(), "fixture never reported its descendant pid"
    grandchild = int(pidfile.read_text().strip())

    result = await driver.cancel(request.run_id, ctx)
    assert result.requested is True
    assert result.confirmed is True, "termination was not confirmed"
    await asyncio.wait_for(task, timeout=15)

    for _ in range(100):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("a descendant that ignores SIGTERM survived the process-group kill")

    assert terminal(events).kind == EventKind.RUN_CANCELLED


# ------------------------------------------------------- failure-injection


class _BrokenWriter:
    """A stdin writer whose first write fails like a CLI that closed its input."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        raise self._exc


class _StubProcess:
    """Minimal SpawnedProcess double.

    ``pid`` is deliberately 0 so the termination helper refuses to signal
    anything: a fake pid must never reach ``killpg`` in a test.
    """

    def __init__(self, stdin, stdout, stderr) -> None:
        self.pid = 0
        self.returncode = None
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr

    async def wait(self) -> int:
        self.returncode = 0
        return 0


class _StubExecutor:
    def __init__(self, process: _StubProcess) -> None:
        self._process = process

    async def spawn(self, argv, *, cwd=None) -> _StubProcess:
        return self._process


def _eof_reader() -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_eof()
    return reader


class _BlockingWriter:
    """A stdin writer whose drain never completes (the CLI never reads)."""

    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        await asyncio.Event().wait()


async def test_stalled_prompt_write_is_bounded_and_never_a_success():
    process = _StubProcess(_BlockingWriter(), _eof_reader(), _eof_reader())
    driver = AntigravityDriver(cli_command="agy")
    started = time.monotonic()
    events = await collect(
        driver, make_request(deadline=0.4), RuntimeContext(executor=_StubExecutor(process))
    )
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, "a stalled prompt write was not bounded by the deadline"
    assert not any(event.kind == EventKind.RUN_COMPLETED for event in events)


class _CancelAwareBrokenWriter:
    """Fails the prompt write only once the run has actually been cancelled."""

    def __init__(self, lookup) -> None:
        self._lookup = lookup

    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        while True:
            event = self._lookup()
            if event is not None and event.is_set():
                raise BrokenPipeError("CLI killed while the prompt was in flight")
            await asyncio.sleep(0.01)


async def test_cancel_during_the_prompt_write_is_not_a_provider_failure():
    driver = AntigravityDriver(cli_command="agy")
    request = make_request(deadline=10.0)
    writer = _CancelAwareBrokenWriter(
        lambda: driver._cancel_events.get(request.run_id)
    )
    process = _StubProcess(writer, _eof_reader(), _eof_reader())
    ctx = RuntimeContext(executor=_StubExecutor(process))
    events: list = []

    async def consume() -> None:
        async for event in driver.execute(request, ctx):
            events.append(event)

    task = asyncio.ensure_future(consume())
    for _ in range(100):
        if events:
            break
        await asyncio.sleep(0.05)
    assert events, "the run never started"

    # The stub has pid 0, so termination can never be confirmed: the documented
    # contract is then to end without a terminal event (the Runner records
    # unknown) rather than to report a provider failure.
    result = await driver.cancel(request.run_id, ctx)
    assert result.confirmed is False
    await asyncio.wait_for(task, timeout=15)
    assert not any(
        event.kind
        in (EventKind.RUN_COMPLETED, EventKind.RUN_FAILED, EventKind.RUN_CANCELLED)
        for event in events
    ), kinds(events)


async def test_broken_stdin_is_a_failed_run_not_an_exception():
    process = _StubProcess(
        _BrokenWriter(BrokenPipeError("stdin closed")), _eof_reader(), _eof_reader()
    )
    driver = AntigravityDriver(cli_command="agy")
    events = await collect(
        driver, make_request(), RuntimeContext(executor=_StubExecutor(process))
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "cli_broken_pipe"


# ------------------------------------------------------------ resource use


async def test_repeated_runs_do_not_leak_descriptors(tmp_path):
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("descriptor accounting requires /proc")
    driver = AntigravityDriver(
        cli_command=str(make_wrapper(tmp_path)), grace_seconds=0.5
    )
    env = dict(os.environ)
    env["FAKE_AGY_MODE"] = "ok"
    executor = LocalProcessExecutor(env=env)
    # Leaked asyncio subprocess transports are reference cycles: a generational
    # collection during the loop could release them and mask the leak, so the
    # collector is disabled for the measurement window.
    gc.collect()
    gc.disable()
    try:
        before = len(os.listdir("/proc/self/fd"))
        for index in range(10):
            request = make_request(run_id=f"run_leak_{index}")
            events = await collect(driver, request, RuntimeContext(executor=executor))
            assert terminal(events).kind == EventKind.RUN_COMPLETED
        after = len(os.listdir("/proc/self/fd"))
    finally:
        gc.enable()
    # One incidental descriptor is tolerated; a leaked transport would show more.
    assert after - before <= 1, f"descriptor growth across runs: {before} -> {after}"
