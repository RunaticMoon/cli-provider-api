"""Real socket regression: silence beyond the former 15s receive timeout.

The peer is synthetic; this is not native CLI/provider evidence.
"""
import asyncio
from datetime import datetime, timezone
import json
import time

import pytest

from cli_provider_core.runner import UdsRunnerSession


@pytest.mark.asyncio
async def test_real_uds_silence_over_15s_within_larger_deadline(tmp_path):
    path = str(tmp_path / "silent.sock")
    finished = asyncio.Event()

    async def peer(reader, writer):
        try:
            request = json.loads(await reader.readline())
            request_id = request["id"]
            run_id = request["params"]["run_id"]
            # No heartbeats or frames: exercise the actual receive wait.
            await asyncio.sleep(16)
            for sequence, kind, payload in (
                (1, "run.started", {"preset": "mock/text"}),
                (2, "message.delta", {"text": "silent fixture"}),
                (3, "run.completed", {"outcome": "succeeded", "usage": {"provenance": "unknown"}}),
            ):
                event = {
                    "schema_version": 1, "run_id": run_id, "sequence": sequence,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "synthetic": True, "kind": kind, "payload": payload,
                }
                writer.write((json.dumps({"v": 1, "type": "event", "request_id": request_id, "event": event}) + "\n").encode())
            result = {
                "run_id": run_id, "status": "completed", "outcome": "succeeded",
                "verification": {"status": "not_run", "source": "synthetic"},
                "usage": {"provenance": "unknown"}, "terminal_kind": "run.completed",
                "terminal_sequence": 3, "events_seen": 3, "synthetic": True,
            }
            writer.write((json.dumps({"v": 1, "type": "response", "id": request_id, "ok": True, "result": result}) + "\n").encode())
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finished.set()

    server = await asyncio.start_unix_server(peer, path=path)
    session = UdsRunnerSession(path)
    started = time.monotonic()
    try:
        async with asyncio.timeout(35):
            events = [event async for event in session.run({
                "run_id": "run_silent", "preset": "mock/text", "deadline_seconds": 25.0,
            })]
        assert time.monotonic() - started >= 15
        assert [event["kind"] for event in events] == ["run.started", "message.delta", "run.completed"]
        assert session.last_result is not None
        assert session.last_result.status.value == "completed"
    finally:
        await session.aclose()
        server.close()
        await server.wait_closed()
        await asyncio.wait_for(finished.wait(), timeout=20)
