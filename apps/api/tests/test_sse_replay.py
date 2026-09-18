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


class _PartialFirstPageStore(Store):
    """First read returns a partial snapshot, like a run still producing events."""

    def __init__(self, path: str, *, first_page: int) -> None:
        super().__init__(path)
        self._first_page = first_page
        self._first = True

    def list_events(self, run_id, *, after=0, limit=1000):
        if self._first:
            self._first = False
            return super().list_events(run_id, after=after, limit=self._first_page)
        return super().list_events(run_id, after=after, limit=limit)


async def test_stream_chat_drains_events_appended_while_yielding(tmp_path):
    # A page shorter than the read limit does NOT mean the run has no further
    # events: the run can persist more while the generator yields the previous
    # page, and it can reach its terminal state in that window. The reader must
    # re-check the durable backlog before ending, otherwise a fast run is
    # delivered only partially (observed on CPython 3.12: 27/38 of 300 deltas).
    store = _PartialFirstPageStore(str(tmp_path / "core.db"), first_page=2)
    store.initialize()
    record = store.reserve(
        run_id="run_partial",
        attempt_id="att_partial",
        principal="alpha",
        task_id="task-partial",
        preset="mock/text",
        driver_id="mock",
        runner_instance="r1",
        workspace_id="ws-alpha",
        request_hash="sha256:y",
        task_policy="text",
        status="running",
    )
    active = ActiveRun(record=record, messages=[])
    store.append_event(
        "run_partial", _event("run_partial", 1, "run.started", {"preset": "mock/text"})
    )
    for index in range(5):
        store.append_event(
            "run_partial",
            _event("run_partial", index + 2, "message.delta", {"text": f"d{index}"}),
        )
    store.append_event(
        "run_partial",
        _event(
            "run_partial",
            7,
            "run.completed",
            {"outcome": "succeeded", "usage": {"provenance": "unknown"}},
        ),
    )
    store.set_attempt("run_partial", status="completed", outcome="succeeded")
    # The run is already terminal when the reader starts yielding.
    active.terminal_status = "completed"

    pieces: list[str] = []
    async for piece in stream_chat(
        request=_Request(),
        active=active,
        store=store,
        chat_id="chatcmpl-run_partial",
        model="mock/text",
        created=0,
        keepalive_seconds=0.05,
    ):
        pieces.append(piece)
    text = "".join(pieces)

    assert text.endswith("data: [DONE]\n\n")
    assert [f'"content":"d{index}"' in text for index in range(5)] == [True] * 5
    store.close()
