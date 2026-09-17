"""POST /v1/chat/completions and the provider-scoped equivalent."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from cli_provider_core import (
    NotFound,
    OperatorConfig,
    PrincipalConfig,
    RunController,
    RunnerRegistry,
    Store,
    chat_id_for_run,
)
from cli_provider_core.models import CANCELLED, COMPLETED, FAILED, UNKNOWN

from .auth import authenticate, check_workspace, resolve_preset
from .errors import ApiRunError
from .schemas import chat_completion, parse_chat_request, read_bounded_json
from .sse import stream_cached, stream_chat
from .views import run_view

router = APIRouter()

_ERROR_CODES = {
    FAILED: ("provider_failed", 502, "the run failed at the provider"),
    UNKNOWN: ("run_unknown", 502, "the run outcome is unknown"),
    CANCELLED: ("run_cancelled", 409, "the run was cancelled"),
}


def _effective_deadline(config: OperatorConfig) -> float:
    return min(
        config.api.default_run_deadline_seconds, config.api.max_run_deadline_seconds
    )


async def _handle(request: Request, driver_id: str | None) -> Any:
    principal: PrincipalConfig = authenticate(request)
    config: OperatorConfig = request.app.state.config
    registry: RunnerRegistry = request.app.state.registry
    controller: RunController = request.app.state.controller
    store: Store = request.app.state.store

    if driver_id is not None and not any(
        r.driver_id == driver_id for r in config.runners
    ):
        raise NotFound("provider not found")

    data = await read_bounded_json(
        request,
        config.api.limits.max_body_bytes,
        timeout_seconds=config.api.request_body_timeout_seconds,
    )
    chat_request = parse_chat_request(data)
    preset, _runner = resolve_preset(
        config=config,
        registry=registry,
        principal=principal,
        alias=chat_request.model,
        driver_scope=driver_id,
    )
    check_workspace(
        config=config, principal=principal, workspace_id=chat_request.workspace_id
    )

    submission = await controller.submit(
        principal=principal.name,
        principal_concurrency=principal.max_concurrency,
        task_id=chat_request.task_id,
        preset=preset,
        workspace_id=chat_request.workspace_id,
        messages=chat_request.messages,
        deadline_seconds=_effective_deadline(config),
    )

    # The standard completion id is deterministically bound to the run, so a
    # caller can identify/cancel the in-flight run even if a gateway drops the
    # initial metadata-only SSE chunk or custom headers.
    chat_id = chat_id_for_run(submission.record.run_id)
    created = int(time.time())

    if submission.cached:
        record = submission.record
        artifacts = store.list_artifacts(record.run_id)
        view = run_view(record, artifacts)
        if chat_request.stream:
            return StreamingResponse(
                stream_cached(
                    record=record,
                    artifacts=artifacts,
                    chat_id=chat_id,
                    model=preset.alias,
                    created=created,
                    summary=record.summary,
                ),
                media_type="text/event-stream",
                headers={"X-Run-Id": record.run_id, "X-Run-Cached": "true"},
            )
        return JSONResponse(
            chat_completion(
                chat_id=chat_id,
                model=preset.alias,
                created=created,
                content=record.summary or "",
                run=view,
                usage=record.usage,
            ),
            headers={"X-Run-Id": record.run_id, "X-Run-Cached": "true"},
        )

    assert submission.active is not None
    run_id = submission.record.run_id

    if chat_request.stream:
        return StreamingResponse(
            stream_chat(
                request=request,
                active=submission.active,
                store=store,
                chat_id=chat_id,
                model=preset.alias,
                created=created,
                keepalive_seconds=config.api.keepalive_seconds,
            ),
            media_type="text/event-stream",
            headers={"X-Run-Id": run_id},
        )

    await submission.active.task
    record = store.get_attempt(run_id)
    assert record is not None
    view = run_view(record, store.list_artifacts(run_id))
    if record.status == COMPLETED:
        return JSONResponse(
            chat_completion(
                chat_id=chat_id,
                model=preset.alias,
                created=created,
                content=record.summary or "",
                run=view,
                usage=record.usage,
            ),
            headers={"X-Run-Id": run_id},
        )
    code, status, message = _ERROR_CODES.get(
        record.status, ("run_error", 502, "the run did not complete successfully")
    )
    raise ApiRunError(message, code=code, http_status=status, run=view)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    return await _handle(request, None)


@router.post("/providers/{driver_id}/v1/chat/completions")
async def chat_completions_scoped(request: Request, driver_id: str) -> Any:
    return await _handle(request, driver_id)
