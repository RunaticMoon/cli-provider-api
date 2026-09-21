"""Authenticated dynamic catalog discovery endpoint.

Every catalog source a principal is allowed to read is filtered by the
operator's grant list, so a never-granted runner is invisible rather than
redacted. Discovery data is read-only — a ``readable`` grant never implies
execution rights; each entry's ``executable`` flag and ``reason`` are the
truthful signal.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from cli_provider_core import NotFound, catalog_view

from .auth import authenticate

router = APIRouter()


async def _catalog_body(request: Request, driver_scope: str | None):
    config = request.app.state.config
    if not config.catalogs:
        raise NotFound("no dynamic catalog sources are configured")
    registry = request.app.state.registry
    # Bounded singleflight refresh: at most one discovery pass per TTL window,
    # so catalog reads never become a per-request spawn storm.
    await registry.ensure_fresh()
    principal = authenticate(request)
    return {
        "object": "list",
        "data": catalog_view(
            config=config,
            registry=registry,
            principal=principal,
            driver_scope=driver_scope,
        ),
    }


@router.get("/api/v1/catalog")
async def list_catalog(request: Request):
    return await _catalog_body(request, None)


@router.get("/providers/{driver_id}/api/v1/catalog")
async def list_provider_catalog(request: Request, driver_id: str):
    return await _catalog_body(request, driver_id)
