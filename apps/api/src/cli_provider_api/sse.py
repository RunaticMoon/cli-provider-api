"""SSE rendering for chat completions.

Only ``message.delta`` is streamed as answer content. run.started, tool events,
permissions, artifacts and usage are never streamed as answer text. The run's
identity and normalized outcome are exposed as a JSON ``run`` metadata extension
on the first and final chunk (a 9Router-preserved extension, since custom
headers may be dropped). Keepalive comments do not extend the run deadline.

Event delivery is durable replay: every accepted event is stored before the
reader sees it, so a late subscriber still gets the full ordered stream and a
slow consumer cannot stall the run.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from cli_provider_core import ActiveRun, AttemptRecord, Store

from .schemas import chat_chunk
from .views import run_view

_EVENT_PAGE = 1000


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _delta_chunk(
    *, chat_id: str, model: str, created: int, text: str
) -> str:
    return _sse(
        chat_chunk(
            chat_id=chat_id,
            model=model,
            created=created,
            content=text,
            finish_reason=None,
        )
    )


async def stream_chat(
    *,
    request: Any,
    active: ActiveRun,
    store: Store,
    chat_id: str,
    model: str,
    created: int,
    keepalive_seconds: float,
) -> AsyncIterator[str]:
    run_id = active.record.run_id
    initial = store.get_attempt(run_id) or active.record
    # First chunk carries run identity in metadata, never answer text.
    yield _sse(
        chat_chunk(
            chat_id=chat_id,
            model=model,
            created=created,
            content=None,
            finish_reason=None,
            run=run_view(initial, []),
        )
    )

    last_sequence = 0
    while True:
        # Clear before draining so a wakeup raised during the drain is not lost.
        active.wakeup.clear()
        drained = 0
        for record in store.list_events(run_id, after=last_sequence, limit=_EVENT_PAGE):
            last_sequence = record.sequence
            drained += 1
            event = record.event
            if event.get("kind") == "message.delta":
                text = str(event.get("payload", {}).get("text", ""))
                if text:
                    yield _delta_chunk(
                        chat_id=chat_id, model=model, created=created, text=text
                    )
        fully_drained = drained < _EVENT_PAGE
        # End detection uses the durable terminal state, not a droppable signal,
        # and only once every persisted event has actually been replayed.
        if active.terminal_status is not None and fully_drained:
            break
        if await request.is_disconnected():
            break
        if not fully_drained:
            # A larger backlog remains; keep draining without waiting.
            continue
        try:
            await asyncio.wait_for(active.wakeup.wait(), timeout=keepalive_seconds)
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"

    final = store.get_attempt(run_id)
    if final is not None and final.status == "completed":
        yield _sse(
            chat_chunk(
                chat_id=chat_id,
                model=model,
                created=created,
                content=None,
                finish_reason="stop",
                run=run_view(final, store.list_artifacts(run_id)),
            )
        )
    elif final is not None:
        yield _sse(
            {
                "error": {
                    "message": "run did not complete successfully",
                    "type": "run_error",
                    "code": final.outcome or final.status,
                    "run_id": run_id,
                },
                "run": run_view(final, store.list_artifacts(run_id)),
            }
        )
    yield "data: [DONE]\n\n"


def stream_cached(
    *,
    record: AttemptRecord,
    artifacts: list[Any],
    chat_id: str,
    model: str,
    created: int,
    summary: str | None,
) -> AsyncIterator[str]:
    view = run_view(record, artifacts)

    async def generator() -> AsyncIterator[str]:
        yield _sse(
            chat_chunk(
                chat_id=chat_id,
                model=model,
                created=created,
                content=None,
                finish_reason=None,
                run=view,
            )
        )
        if summary:
            yield _delta_chunk(
                chat_id=chat_id, model=model, created=created, text=summary
            )
        yield _sse(
            chat_chunk(
                chat_id=chat_id,
                model=model,
                created=created,
                content=None,
                finish_reason="stop",
                run=view,
            )
        )
        yield "data: [DONE]\n\n"

    return generator()
