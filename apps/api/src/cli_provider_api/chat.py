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
from cli_provider_core.models import (
    CANCELLED,
    COMPLETED,
    FAILED,
    OUTCOME_QUEUE_TIMEOUT,
    OUTCOME_REJECTED,
    UNKNOWN,
)

from .auth import authenticate, check_workspace, resolve_preset
from .errors import ApiRunError
from .schemas import chat_completion, parse_chat_request, read_bounded_json
from .sse import stream_cached, stream_chat
from .views import run_view

router = APIRouter()

# HTTP classification is outcome-specific: a failed run where the provider never
# executed must not be reported as a provider failure. The body's run view stays
# authoritative; only the code/status/message are normalized.
_STATUS_FAILURES = {
    FAILED: ("provider_failed", 502, "run_error", "the run failed at the provider"),
    UNKNOWN: ("run_unknown", 502, "run_error", "the run outcome is unknown"),
    CANCELLED: ("run_cancelled", 409, "run_error", "the run was cancelled"),
}
_OUTCOME_FAILURES = {
    OUTCOME_QUEUE_TIMEOUT: (
        "queue_timeout",
        429,
        "rate_limit_error",
        "the run timed out waiting for runner capacity",
    ),
    OUTCOME_REJECTED: (
        "run_rejected",
        502,
        "run_error",
        "the runner rejected the run before it executed",
    ),
}


def run_error_classification(status: str, outcome: str | None) -> tuple[str, int, str, str]:
    """Return ``(code, http_status, error_type, message)`` for a failed run."""
    if outcome in _OUTCOME_FAILURES:
        return _OUTCOME_FAILURES[outcome]
    return _STATUS_FAILURES.get(
        status, ("run_error", 502, "run_error", "the run did not complete successfully")
    )


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
        execution=chat_request.execution,
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
    code, http_status, error_type, message = run_error_classification(
        record.status, record.outcome
    )
    raise ApiRunError(
        message,
        code=code,
        http_status=http_status,
        run=view,
        error_type=error_type,
    )


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    return await _handle(request, None)


@router.post("/providers/{driver_id}/v1/chat/completions")
async def chat_completions_scoped(request: Request, driver_id: str) -> Any:
    return await _handle(request, driver_id)
