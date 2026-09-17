"""Run/event/cancel/artifact routes. Every lookup is ownership- and scope-checked."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from cli_provider_core import (
    ArtifactRecord,
    NotFound,
    OperatorConfig,
    RunController,
    Store,
)
from cli_provider_core.ids import is_safe_id

from .auth import authenticate
from .views import run_view

router = APIRouter()


@router.get("/api/v1/runs/{run_id}")
async def get_run(request: Request, run_id: str) -> dict[str, Any]:
    principal = authenticate(request)
    controller: RunController = request.app.state.controller
    store: Store = request.app.state.store
    record = controller.owned_attempt(run_id, principal.name)
    return run_view(record, store.list_artifacts(record.run_id))


@router.get("/api/v1/runs/{run_id}/events")
async def get_events(
    request: Request,
    run_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, ge=1, le=10000),
) -> dict[str, Any]:
    principal = authenticate(request)
    controller: RunController = request.app.state.controller
    record, events = controller.events(
        run_id, principal.name, after=after, limit=limit
    )
    return {
        "run_id": record.run_id,
        "status": record.status,
        "events": [event.event for event in events],
    }


@router.post("/api/v1/runs/{run_id}/cancel")
async def cancel_run(request: Request, run_id: str) -> dict[str, Any]:
    principal = authenticate(request)
    controller: RunController = request.app.state.controller
    view = await controller.cancel(run_id, principal.name)
    return {
        "run_id": view.run_id,
        "status": view.status,
        "requested": view.requested,
        "confirmed": view.confirmed,
        "detail": view.detail,
    }


@router.get("/api/v1/artifacts/{artifact_id}")
async def get_artifact(request: Request, artifact_id: str) -> Response:
    principal = authenticate(request)
    controller: RunController = request.app.state.controller
    config: OperatorConfig = request.app.state.config

    if not is_safe_id(artifact_id):
        raise NotFound("artifact not found")
    record: ArtifactRecord = controller.owned_artifact(artifact_id, principal.name)

    root = os.path.realpath(config.artifacts_dir())
    real = os.path.realpath(record.path)
    if not real.startswith(root + os.sep):
        raise NotFound("artifact not found")
    if record.size > config.api.limits.max_artifact_bytes:
        raise NotFound("artifact not found")
    try:
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise NotFound("artifact not found") from exc
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(config.api.limits.max_artifact_bytes + 1)
    if len(data) > config.api.limits.max_artifact_bytes:
        raise NotFound("artifact not found")
    return Response(content=data, media_type=record.content_type)
