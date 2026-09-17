from datetime import datetime, timezone

import pytest

from cli_provider_core import UpstreamProtocolError
from cli_provider_core.runner import UdsRunnerSession


def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def event_frame(run_id: str, sequence: int, kind: str, payload: dict, *, request_id: str = "rid-1", v: int = 1):
    return {
        "v": v,
        "type": "event",
        "request_id": request_id,
        "event": {
            "schema_version": 1,
            "run_id": run_id,
            "sequence": sequence,
            "timestamp": ts(),
            "synthetic": True,
            "kind": kind,
            "payload": payload,
        },
    }


def result_frame(run_id: str, *, request_id: str = "rid-1", status="completed", outcome="succeeded"):
    return {
        "v": 1,
        "type": "response",
        "id": request_id,
        "ok": True,
        "result": {
            "run_id": run_id,
            "status": status,
            "outcome": outcome,
            "verification": {"status": "not_run", "source": "fake"},
            "usage": {"provenance": "unknown"},
            "terminal_kind": "run.completed",
            "terminal_sequence": 2,
            "events_seen": 2,
            "synthetic": True,
        },
    }


class FakeClient:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent: list[tuple] = []
        self.closed = False

    async def send_request(self, method, params=None, request_id=None):
        rid = request_id or "rid-1"
        self.sent.append((method, params, rid))
        return rid

    async def recv(self):
        return self.frames.pop(0) if self.frames else None

    async def aclose(self):
        self.closed = True


def session_with(frames) -> tuple[UdsRunnerSession, FakeClient]:
    session = UdsRunnerSession("/does/not/matter")
    client = FakeClient(frames)
    session._client = client  # inject the bounded transport double
    return session, client


PARAMS = {"run_id": "run_1", "preset": "mock/text"}


async def test_valid_stream_is_accepted():
    session, _ = session_with(
        [
            event_frame("run_1", 1, "run.started", {"preset": "mock/text"}),
            event_frame("run_1", 2, "message.delta", {"text": "hi"}),
            result_frame("run_1"),
        ]
    )
    events = [event async for event in session.run(PARAMS)]
    assert [e["kind"] for e in events] == ["run.started", "message.delta"]
    assert session.last_result is not None
    assert session.last_result.status.value == "completed"


async def test_mismatched_request_id_is_rejected():
    session, _ = session_with(
        [event_frame("run_1", 1, "run.started", {"preset": "mock/text"}, request_id="other")]
    )
    with pytest.raises(UpstreamProtocolError):
        async for _ in session.run(PARAMS):
            pass


async def test_mismatched_run_id_is_rejected():
    session, _ = session_with(
        [event_frame("different", 1, "run.started", {"preset": "mock/text"})]
    )
    with pytest.raises(UpstreamProtocolError):
        async for _ in session.run(PARAMS):
            pass


async def test_non_contiguous_sequence_is_rejected():
    session, _ = session_with(
        [
            event_frame("run_1", 1, "run.started", {"preset": "mock/text"}),
            event_frame("run_1", 3, "message.delta", {"text": "x"}),
        ]
    )
    with pytest.raises(UpstreamProtocolError):
        async for _ in session.run(PARAMS):
            pass


async def test_invalid_event_schema_is_rejected():
    session, _ = session_with([event_frame("run_1", 1, "run.started", {})])
    with pytest.raises(UpstreamProtocolError):
        async for _ in session.run(PARAMS):
            pass


async def test_protocol_version_mismatch_is_rejected():
    session, _ = session_with([event_frame("run_1", 1, "run.started", {}, v=2)])
    with pytest.raises(UpstreamProtocolError):
        async for _ in session.run(PARAMS):
            pass


async def test_unrelated_response_is_rejected():
    session, _ = session_with([result_frame("run_1", request_id="someone-else")])
    with pytest.raises(UpstreamProtocolError):
        async for _ in session.run(PARAMS):
            pass


async def test_unexpected_frame_type_is_rejected():
    session, _ = session_with([{"v": 1, "type": "garbage", "id": "rid-1"}])
    with pytest.raises(UpstreamProtocolError):
        async for _ in session.run(PARAMS):
            pass


def test_run_recv_timeout_exceeds_15s_for_a_larger_deadline():
    # A 60 s run deadline must not be overridden by a fixed 15 s frame timeout.
    session = UdsRunnerSession("/does/not/matter")
    assert session._run_recv_timeout({"run_id": "run_1", "deadline_seconds": 60.0}) > 15.0
    # The controller-derived run budget always wins when set.
    session.set_run_timeout(70.0)
    assert session._run_recv_timeout({"run_id": "run_1", "deadline_seconds": 60.0}) == 70.0


async def test_manifest_is_validated_against_the_sdk_schema():
    session, _ = session_with(
        [
            {
                "v": 1,
                "type": "response",
                "id": "rid-1",
                "ok": True,
                "result": {"driver_id": "mock", "name": "Mock"},  # incomplete manifest
            }
        ]
    )
    with pytest.raises(UpstreamProtocolError):
        await session.manifest()


async def test_model_descriptor_is_validated_against_the_sdk_schema():
    session, _ = session_with(
        [
            {
                "v": 1,
                "type": "response",
                "id": "rid-1",
                "ok": True,
                "result": {"models": [{"model_id": "mock-model"}]},  # missing fields
            }
        ]
    )
    with pytest.raises(UpstreamProtocolError):
        await session.discover_models()


async def test_manifest_and_discover_validate_the_response_id():
    session, _ = session_with(
        [
            {
                "v": 1,
                "type": "response",
                "id": "not-rid-1",
                "ok": True,
                "result": {"driver_id": "mock"},
            }
        ]
    )
    with pytest.raises(UpstreamProtocolError):
        await session.manifest()
