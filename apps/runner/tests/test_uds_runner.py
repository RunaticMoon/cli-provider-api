import asyncio
import contextlib
import json
import os
import stat
import time
from datetime import datetime, timezone
from typing import AsyncIterator

import pytest

from cli_provider_sdk import (
    SDK_VERSION,
    Capabilities,
    DriverManifest,
    Message,
    MessageDeltaEvent,
    MessageDeltaPayload,
    ModelDescriptor,
    NormalizedRequest,
    ProbeReport,
    ProviderDriver,
    RunCompletedEvent,
    RunCompletedPayload,
    RunEvent,
    RunStartedEvent,
    RunStartedPayload,
    RuntimeContext,
    ToolStartedEvent,
    ToolStartedPayload,
    TransportKind,
    Usage,
    Verification,
    WorkspaceRef,
)
from cli_provider_runner import RunnerServer
from cli_provider_runner.client import RunnerClient

from conftest import run_params


async def drive(client: RunnerClient, params: dict) -> list:
    events = []
    async for envelope in client.run(params):
        events.append(envelope)
    return events


async def raw_roundtrip(socket_path: str, payload: bytes, timeout: float = 5.0) -> dict:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write(payload)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(65536), timeout=timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    assert data, "runner sent no response to a rejected frame"
    return json.loads(data.split(b"\n", 1)[0])


# A driver defined directly against the public types, used to prove the Runner
# needs no API-specific adaptation and forwards structural events faithfully.
class UserShapeDriver:
    def __init__(self) -> None:
        self.execute_calls = 0

    @property
    def manifest(self) -> DriverManifest:
        return DriverManifest(
            driver_id="user-shape",
            name="User Shape Driver",
            version="1.0.0",
            sdk_version=SDK_VERSION,
            protocol_family="user-shape",
            supported_transports=[TransportKind.STDIO],
        )

    async def probe(self, ctx: RuntimeContext) -> ProbeReport:
        return ProbeReport(
            ok=True,
            driver_id="user-shape",
            driver_version="1.0.0",
            capabilities=Capabilities(
                streaming="native",
                sessions="none",
                roles="serialized",
                structured_output="none",
                external_tool_calls=False,
                internal_tools=True,
                vision=False,
                workspace_write=False,
                web_search=False,
                usage="unknown",
            ),
        )

    async def discover_models(self, ctx: RuntimeContext) -> list[ModelDescriptor]:
        return [
            ModelDescriptor(
                model_id="user-shape-model",
                display_name="User Shape Model",
                verification=Verification(status="unknown", source="user-shape"),
            )
        ]

    def execute(
        self, request: NormalizedRequest, ctx: RuntimeContext
    ) -> AsyncIterator[RunEvent]:
        self.execute_calls += 1

        async def generator() -> AsyncIterator[RunEvent]:
            now = datetime.now(timezone.utc)
            yield RunStartedEvent(
                run_id=request.run_id,
                sequence=1,
                timestamp=now,
                payload=RunStartedPayload(preset=request.preset),
            )
            yield MessageDeltaEvent(
                run_id=request.run_id,
                sequence=2,
                timestamp=now,
                payload=MessageDeltaPayload(text="visible answer"),
            )
            yield ToolStartedEvent(
                run_id=request.run_id,
                sequence=3,
                timestamp=now,
                payload=ToolStartedPayload(tool_call_id="t1", name="read_file"),
            )
            yield RunCompletedEvent(
                run_id=request.run_id,
                sequence=4,
                timestamp=now,
                payload=RunCompletedPayload(
                    outcome="partial", usage=Usage(provenance="unknown")
                ),
            )

        return generator()

    async def cancel(self, run_id: str, ctx: RuntimeContext):
        raise AssertionError("cancel not expected in these tests")

    async def aclose(self) -> None:
        return None


@contextlib.asynccontextmanager
async def inproc_server(tmp_path, driver, **kwargs):
    socket_path = str(tmp_path / "s.sock")
    server = RunnerServer(
        socket_path=socket_path, instance_id="inproc", driver=driver, **kwargs
    )
    server.load()
    task = asyncio.create_task(server.serve())
    try:
        ready = False
        for _ in range(300):
            if os.path.exists(socket_path):
                try:
                    client = await RunnerClient.connect(socket_path)
                    await client.aclose()
                    ready = True
                    break
                except OSError:
                    pass
            await asyncio.sleep(0.01)
        if not ready:
            raise TimeoutError("in-process runner socket never became ready")
        yield socket_path
    finally:
        server.request_stop()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, Exception):
            task.cancel()


async def test_manifest_probe_and_discover_over_uds(runner_factory):
    runner = runner_factory("success")
    client = await RunnerClient.connect(runner.socket_path)
    try:
        manifest = await client.call("manifest")
        assert manifest.ok and manifest.result["driver_id"] == "mock"
        assert manifest.result["synthetic"] is True
        assert manifest.result["sdk_version"] == SDK_VERSION
        assert manifest.result["supported_transports"] == ["stdio"]

        probe = await client.call("probe")
        assert probe.ok
        assert probe.result["driver_id"] == "mock"
        assert probe.result["cli_version"] is None
        assert probe.result["capabilities"]["external_tool_calls"] is False
        assert probe.result["capabilities"]["structured_output"] == "none"
        assert probe.result["capabilities"]["vision"] is False

        models = await client.call("discover_models")
        assert models.ok
        assert models.result["models"][0]["model_id"] == "mock-model"
        assert models.result["models"][0]["verification"]["status"] != "passed"

        runtime = await client.call("runtime")
        assert runtime.ok
        assert runtime.result["max_parallel_runs"] == 1
        assert runtime.result["max_queue"] >= 1
        assert runtime.result["cancel_cleanup_seconds"] > 0
    finally:
        await client.aclose()


async def test_runtime_declares_the_bounded_cancel_cleanup_budget(runner_factory):
    # Worst case the Runner can spend unwinding after a run deadline is two
    # bounded cancel waits; the API derives its outer budget from this value.
    runner = runner_factory("success", cancel_deadline=6.0)
    client = await RunnerClient.connect(runner.socket_path)
    try:
        runtime = await client.call("runtime")
        assert runtime.ok
        assert runtime.result["cancel_cleanup_seconds"] == 12.0
    finally:
        await client.aclose()


async def test_success_run_validates_single_terminal_and_result(runner_factory):
    runner = runner_factory("success")
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(client, run_params())
        response = client.last_run_response
        assert response is not None and response.ok
        result = response.result
        assert result["status"] == "completed"
        assert result["outcome"] == "succeeded"
        assert result["verification"]["status"] == "not_run"
        assert result["usage"]["provenance"] == "unknown"
        assert result["usage"]["input_tokens"] is None
        assert result["usage"]["output_tokens"] is None
        assert result["synthetic"] is True

        kinds = [envelope.event.kind for envelope in events]
        assert kinds[0] == "run.started"
        assert kinds[-1] == "run.completed"
        assert kinds.count("run.completed") == 1
        assert "run.failed" not in kinds and "run.cancelled" not in kinds
        assert "message.delta" in kinds
        sequences = [envelope.event.sequence for envelope in events]
        assert sequences == list(range(1, len(events) + 1))
        assert all(envelope.event.schema_version == 1 for envelope in events)
        assert all(envelope.event.run_id == "run-1" for envelope in events)
        assert all(envelope.event.synthetic for envelope in events)
    finally:
        await client.aclose()


async def test_completed_partial_is_preserved_end_to_end(runner_factory):
    runner = runner_factory("partial")
    client = await RunnerClient.connect(runner.socket_path)
    try:
        await drive(client, run_params())
        result = client.last_run_response.result
        assert result["status"] == "completed"
        assert result["outcome"] == "partial"
        assert result["verification"]["status"] == "not_run"
    finally:
        await client.aclose()


async def test_exact_slash_aliases_round_trip_over_uds(runner_factory):
    runner = runner_factory("success")
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client, run_params(preset="agy/review", model_alias="mock/text")
        )
        started = events[0].event
        assert started.kind == "run.started"
        assert started.payload.preset == "agy/review"
        assert started.payload.model_alias == "mock/text"
        assert client.last_run_response.result["status"] == "completed"

        bad = await client.call("run", run_params(preset="a/../b"))
        assert bad.ok is False and bad.error.code == "INVALID_PARAMS"
    finally:
        await client.aclose()


async def test_explicit_failure_reports_failed_not_success(runner_factory):
    runner = runner_factory("failed")
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(client, run_params())
        response = client.last_run_response
        assert response is not None and response.ok
        assert response.result["status"] == "failed"
        assert response.result["outcome"] == "provider_error"
        assert response.result["terminal_kind"] == "run.failed"
        assert events[-1].event.kind == "run.failed"
    finally:
        await client.aclose()


async def test_abrupt_crash_is_unknown_not_completed(runner_factory):
    runner = runner_factory("crash")
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(client, run_params())
        response = client.last_run_response
        assert response is not None and response.ok
        assert response.result["status"] == "unknown"
        assert response.result["outcome"] == "unknown"
        assert response.result["terminal_kind"] is None
        assert response.result["verification"]["status"] == "unknown"
        # deltas were emitted before the crash, but no completion is claimed
        assert [event.event.kind for event in events][-1] == "message.delta"
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "mode",
    ["duplicate_sequence", "event_after_terminal", "no_terminal"],
)
async def test_malformed_driver_sequence_is_unknown(runner_factory, mode):
    runner = runner_factory("malformed", malformed_mode=mode)
    client = await RunnerClient.connect(runner.socket_path)
    try:
        await drive(client, run_params())
        response = client.last_run_response
        assert response is not None and response.ok
        assert response.result["status"] == "unknown"
        assert response.result["outcome"] == "unknown"
        assert response.result["terminal_kind"] is None
    finally:
        await client.aclose()


async def test_cancel_reports_requested_and_confirmed(runner_factory):
    runner = runner_factory("hang")
    run_client = await RunnerClient.connect(runner.socket_path)
    cancel_client = await RunnerClient.connect(runner.socket_path)
    try:
        task = asyncio.create_task(drive(run_client, run_params()))
        await asyncio.sleep(0.3)

        response = await cancel_client.call("cancel", {"run_id": "run-1"})
        assert response.ok
        cancel = response.result
        assert cancel["requested"] is True
        assert cancel["confirmed"] is True
        assert cancel["requested_at"] is not None
        assert cancel["confirmed_at"] is not None
        assert cancel["deadline_seconds"] > 0

        await asyncio.wait_for(task, timeout=5)
        result = run_client.last_run_response
        assert result is not None and result.ok
        assert result.result["status"] == "cancelled"
        assert result.result["outcome"] == "cancelled"
        assert result.result["terminal_kind"] == "run.cancelled"
    finally:
        await run_client.aclose()
        await cancel_client.aclose()


async def test_cancel_of_unknown_run_is_not_confirmed(runner_factory):
    runner = runner_factory("success")
    client = await RunnerClient.connect(runner.socket_path)
    try:
        response = await client.call("cancel", {"run_id": "does-not-exist"})
        assert response.ok
        assert response.result["requested"] is False
        assert response.result["confirmed"] is False
    finally:
        await client.aclose()


async def test_hang_deadline_is_enforced_mid_stream(runner_factory):
    runner = runner_factory("hang")
    client = await RunnerClient.connect(runner.socket_path)
    try:
        started = time.monotonic()
        await drive(client, run_params(deadline_seconds=0.5))
        elapsed = time.monotonic() - started
        result = client.last_run_response.result
        assert elapsed < 5.0
        assert result["status"] != "completed"
        assert result["outcome"] != "succeeded"
        assert result["status"] == "cancelled"
    finally:
        await client.aclose()


async def test_deadline_cancelled_detail_carries_the_driver_detail(runner_factory):
    runner = runner_factory("hang")
    client = await RunnerClient.connect(runner.socket_path)
    try:
        await drive(client, run_params(deadline_seconds=0.5))
        result = client.last_run_response.result
        assert result["status"] == "cancelled"
        # The driver's own bounded detail must survive: it is where a descendant
        # that was deliberately not chased is reported to the operator.
        assert result["detail"] == "mock driver acknowledged cancellation request"
    finally:
        await client.aclose()


async def test_deadline_cancelled_detail_is_truncated_when_the_driver_detail_is_long(
    runner_factory,
):
    long_detail = "d" * 500
    runner = runner_factory("hang", cancel_detail=long_detail)
    client = await RunnerClient.connect(runner.socket_path)
    try:
        await drive(client, run_params(deadline_seconds=0.5))
        result = client.last_run_response.result
        assert result["status"] == "cancelled"
        assert result["detail"] == long_detail[:200]
        assert len(result["detail"]) == 200
    finally:
        await client.aclose()


async def test_streaming_events_cannot_renew_the_deadline(runner_factory):
    runner = runner_factory("slow")
    client = await RunnerClient.connect(runner.socket_path)
    try:
        started = time.monotonic()
        events = await drive(client, run_params(deadline_seconds=0.5))
        elapsed = time.monotonic() - started
        result = client.last_run_response.result
        # The mock would stream for ~6s if the deadline could be renewed.
        assert elapsed < 3.0
        assert len(events) < 10
        assert result["status"] != "completed"
    finally:
        await client.aclose()


async def test_ignored_cancellation_yields_unknown_not_completed(runner_factory):
    runner = runner_factory("hang_ignores_cancel", cancel_deadline=1.0)
    client = await RunnerClient.connect(runner.socket_path)
    try:
        await drive(client, run_params(deadline_seconds=0.5))
        result = client.last_run_response.result
        assert result["status"] == "unknown"
        assert result["outcome"] == "unknown"
        assert result["terminal_kind"] is None
    finally:
        await client.aclose()


async def test_queued_run_cancellation_never_starts_driver(runner_factory):
    # `hang` emits run.started before blocking: if the queued driver executed at
    # all we would see that event. A queued cancel must produce zero events.
    runner = runner_factory("hang", max_queue=1)
    first = await RunnerClient.connect(runner.socket_path)
    second = await RunnerClient.connect(runner.socket_path)
    canceller = await RunnerClient.connect(runner.socket_path)
    try:
        first_task = asyncio.create_task(drive(first, run_params("run-1")))
        await asyncio.sleep(0.3)
        second_task = asyncio.create_task(drive(second, run_params("run-2")))
        await asyncio.sleep(0.3)

        response = await canceller.call("cancel", {"run_id": "run-2"})
        assert response.ok
        assert response.result["requested"] is True
        assert response.result["confirmed"] is True
        assert "never executed" in response.result["detail"]

        second_events = await asyncio.wait_for(second_task, timeout=5)
        assert second_events == []
        result = second.last_run_response.result
        assert result["status"] == "cancelled"
        assert result["events_seen"] == 0

        # run-1 (a real hang) is still executing and must be cancelled normally.
        await canceller.call("cancel", {"run_id": "run-1"})
        await asyncio.wait_for(first_task, timeout=5)
    finally:
        await first.aclose()
        await second.aclose()
        await canceller.aclose()


async def test_tool_events_are_forwarded_as_tool_not_answer(runner_factory, tmp_path):
    driver = UserShapeDriver()
    assert isinstance(driver, ProviderDriver)
    async with inproc_server(tmp_path, driver) as socket_path:
        client = await RunnerClient.connect(socket_path)
        try:
            events = await drive(client, run_params(preset="mock/text"))
            kinds = [event.event.kind for event in events]
            assert kinds == [
                "run.started",
                "message.delta",
                "tool.started",
                "run.completed",
            ]
            assert events[2].event.payload.name == "read_file"
            result = client.last_run_response.result
            assert result["status"] == "completed"
            assert result["outcome"] == "partial"
            assert driver.execute_calls == 1
        finally:
            await client.aclose()


async def test_oversize_frame_is_rejected(runner_factory):
    runner = runner_factory("success", max_frame_bytes=4096)
    payload = (
        b'{"v":1,"type":"request","id":"big","method":"probe","params":{"pad":"'
        + b"x" * 8000
        + b'"}}\n'
    )
    response = await raw_roundtrip(runner.socket_path, payload)
    assert response["ok"] is False
    assert response["error"]["code"] == "FRAME_TOO_LARGE"


async def test_malformed_json_frame_is_rejected(runner_factory):
    runner = runner_factory("success")
    response = await raw_roundtrip(
        runner.socket_path, b'{"v":1,"type":"request","id":"x","method":probe}\n'
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "MALFORMED_REQUEST"


async def test_unknown_method_is_rejected(runner_factory):
    runner = runner_factory("success")
    client = await RunnerClient.connect(runner.socket_path)
    try:
        response = await client.call("rm -rf /")
        assert response.ok is False
        assert response.error.code == "UNKNOWN_METHOD"
    finally:
        await client.aclose()


async def test_run_request_cannot_select_executable_or_package(runner_factory):
    runner = runner_factory("success")
    client = await RunnerClient.connect(runner.socket_path)
    try:
        response = await client.call("run", run_params(package="evil-driver"))
        assert response.ok is False
        assert response.error.code == "INVALID_PARAMS"

        response = await client.call("run", run_params(cwd="/etc"))
        assert response.ok is False
        assert response.error.code == "INVALID_PARAMS"
    finally:
        await client.aclose()


async def test_abrupt_client_disconnect_does_not_kill_runner(runner_factory):
    runner = runner_factory("hang")
    client = await RunnerClient.connect(runner.socket_path)
    await client.send_request("run", run_params(), request_id="disc-1")
    await asyncio.sleep(0.3)
    client.abort()
    await asyncio.sleep(0.3)

    fresh = await RunnerClient.connect(runner.socket_path)
    try:
        probe = await fresh.call("probe")
        assert probe.ok
    finally:
        await fresh.aclose()
    assert runner.proc.poll() is None


async def test_abrupt_disconnect_mid_stream_never_reports_success(runner_factory):
    runner = runner_factory("slow")
    client = await RunnerClient.connect(runner.socket_path)
    await client.send_request("run", run_params(), request_id="disc-2")
    await asyncio.sleep(0.4)
    client.abort()

    fresh = await RunnerClient.connect(runner.socket_path)
    try:
        probe = await fresh.call("probe")
        assert probe.ok
    finally:
        await fresh.aclose()
    assert runner.proc.poll() is None


async def test_bounded_queue_rejects_when_full(runner_factory):
    runner = runner_factory("hang", max_queue=1)
    first = await RunnerClient.connect(runner.socket_path)
    second = await RunnerClient.connect(runner.socket_path)
    third = await RunnerClient.connect(runner.socket_path)
    canceller = await RunnerClient.connect(runner.socket_path)
    try:
        first_task = asyncio.create_task(drive(first, run_params("run-1")))
        await asyncio.sleep(0.3)
        second_task = asyncio.create_task(drive(second, run_params("run-2")))
        await asyncio.sleep(0.3)

        response = await third.call("run", run_params("run-3"))
        assert response.ok is False
        assert response.error.code == "QUEUE_FULL"

        await canceller.call("cancel", {"run_id": "run-1"})
        await asyncio.wait_for(first_task, timeout=5)

        await asyncio.sleep(0.3)
        await canceller.call("cancel", {"run_id": "run-2"})
        await asyncio.wait_for(second_task, timeout=5)
    finally:
        await first.aclose()
        await second.aclose()
        await third.aclose()
        await canceller.aclose()


async def test_duplicate_run_id_is_rejected_while_active(runner_factory):
    runner = runner_factory("hang")
    first = await RunnerClient.connect(runner.socket_path)
    second = await RunnerClient.connect(runner.socket_path)
    canceller = await RunnerClient.connect(runner.socket_path)
    try:
        task = asyncio.create_task(drive(first, run_params("run-1")))
        await asyncio.sleep(0.3)
        response = await second.call("run", run_params("run-1"))
        assert response.ok is False
        assert response.error.code == "RUN_ALREADY_ACTIVE"
        await canceller.call("cancel", {"run_id": "run-1"})
        await asyncio.wait_for(task, timeout=5)
    finally:
        await first.aclose()
        await second.aclose()
        await canceller.aclose()


def test_runner_socket_is_private_even_with_permissive_umask(runner_factory):
    # The socket must never be group/world-connectable, even when the caller's
    # umask is fully permissive.
    runner = runner_factory("success", umask=0)
    mode = stat.S_IMODE(os.stat(runner.socket_path).st_mode)
    assert mode == 0o600


async def test_inproc_server_private_socket_and_umask_restored(tmp_path):
    old = os.umask(0)
    server = RunnerServer(
        socket_path=str(tmp_path / "perm.sock"),
        instance_id="perm",
        driver=UserShapeDriver(),
    )
    server.load()
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(300):
            if os.path.exists(server.socket_path):
                break
            await asyncio.sleep(0.01)
        mode = stat.S_IMODE(os.stat(server.socket_path).st_mode)
        assert mode == 0o600
        # The narrowed umask was restored: a fresh file is created word-writable.
        probe = tmp_path / "probe"
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        os.close(fd)
        assert stat.S_IMODE(os.stat(probe).st_mode) == 0o666
    finally:
        server.request_stop()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.TimeoutError, Exception):
            task.cancel()
        os.umask(old)


def test_message_helper_import_is_stable():
    # Guards the public Message shape used by RunParams and NormalizedRequest.
    assert Message(role="user", content="x").content == "x"
    assert WorkspaceRef(workspace_id="ws-1").workspace_id == "ws-1"
