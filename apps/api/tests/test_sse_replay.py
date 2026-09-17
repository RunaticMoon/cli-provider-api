"""SSE delivery is durable replay: a slow reader still gets every event and a
terminal end, and a late subscriber can never miss startup deltas."""

from __future__ import annotations

import asyncio
import json

from cli_provider_core import ActiveRun, Store

from cli_provider_api.sse import stream_chat


class _Request:
    async def is_disconnected(self) -> bool:
        return False


def _event(run_id: str, sequence: int, kind: str, payload: dict) -> dict:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "sequence": sequence,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "synthetic": True,
        "kind": kind,
        "payload": payload,
    }


async def test_stream_chat_replays_every_delta_for_a_slow_reader(tmp_path):
    store = Store(str(tmp_path / "core.db"))
    store.initialize()
    record = store.reserve(
        run_id="run_slow",
        attempt_id="att_slow",
        principal="alpha",
        task_id="task-slow",
        preset="mock/text",
        driver_id="mock",
        runner_instance="r1",
        workspace_id="ws-alpha",
        request_hash="sha256:x",
        task_policy="text",
        status="running",
    )
    active = ActiveRun(record=record, messages=[])
    for index in range(300):
        store.append_event(
            "run_slow", _event("run_slow", index + 1, "message.delta", {"text": f"d{index}"})
        )
    store.append_event(
        "run_slow",
        _event(
            "run_slow",
            301,
            "run.completed",
            {"outcome": "succeeded", "usage": {"provenance": "unknown"}},
        ),
    )
    store.set_attempt("run_slow", status="completed", outcome="succeeded")
    active.terminal_status = "completed"

    pieces: list[str] = []
    async for piece in stream_chat(
        request=_Request(),
        active=active,
        store=store,
        chat_id="chatcmpl-run_slow",
        model="mock/text",
        created=0,
        keepalive_seconds=0.05,
    ):
        pieces.append(piece)
        await asyncio.sleep(0.001)  # deliberately slow consumer
    text = "".join(pieces)

    assert text.endswith("data: [DONE]\n\n")
    # Every accepted delta is preserved in order, including the first one.
    assert text.count('"content":"d') == 300
    assert text.index('"content":"d0"') < text.index('"content":"d299"')

    data_lines = [
        line[len("data: ") :]
        for line in text.splitlines()
        if line.startswith("data: ") and "[DONE]" not in line
    ]
    first = json.loads(data_lines[0])
    assert first["run"]["run_id"] == "run_slow"
    assert first["choices"][0]["delta"].get("content") is None
    last = json.loads(data_lines[-1])
    assert last["run"]["status"] == "completed"
    assert last["choices"][0]["finish_reason"] == "stop"
    store.close()
