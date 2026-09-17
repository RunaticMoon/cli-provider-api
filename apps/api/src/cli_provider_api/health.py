"""Liveness and readiness.

Unauthenticated callers get only a status; the detailed internal topology map
(runner ids, preset aliases, driver ids/versions, discovered models, failure
details) requires a valid API key. Model/run isolation is unaffected.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from cli_provider_core import AuthenticationError

from .auth import authenticate

router = APIRouter()


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


def _authenticated_or_none(request: Request):
    try:
        return authenticate(request)
    except AuthenticationError:
        return None


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    registry = request.app.state.registry
    store = request.app.state.store
    ok = registry.ready_status()
    payload: dict[str, object] = {"status": "ready" if ok else "not_ready"}
    if _authenticated_or_none(request) is not None:
        _ok, detail = registry.ready()
        detail["store"] = {"ok": True, "last_reconcile": store.get_meta("last_reconcile")}
        payload["detail"] = detail
    return JSONResponse(payload, status_code=200 if ok else 503)
