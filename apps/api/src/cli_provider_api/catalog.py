"""Authenticated dynamic catalog discovery endpoint.

Every catalog source a principal is allowed to read is filtered by the
operator's grant list, so a never-granted runner is invisible rather than
redacted. Discovery data is read-only — a ``readable`` grant never implies
execution rights; each entry's ``executable`` flag and ``reason`` are the
truthful signal.

Ordering matters: authentication and the driver-scope check run before any
registry refresh, and the bounded refresh only runs when the caller can
actually read at least one in-scope source. An invalid key, a never-granted
principal, or an unknown provider must cause zero runner discovery RPCs —
reads are never a vehicle for unauthenticated driver work.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from cli_provider_core import (
    NotFound,
    catalog_refresh_refs,
    catalog_view,
)

from .auth import authenticate

router = APIRouter()


async def _catalog_body(request: Request, driver_scope: str | None):
    config = request.app.state.config
    if not config.catalogs:
        raise NotFound("no dynamic catalog sources are configured")
    principal = authenticate(request)
    registry = request.app.state.registry
    if driver_scope is not None and not any(
        runner.driver_id == driver_scope for runner in config.runners
    ):
        # Consistent with /providers/{driver_id}/v1/models: an unknown driver
        # scope is a 404, never a silent empty catalog.
        raise NotFound("provider not found")
    # Bounded singleflight refresh — at most one discovery pass per TTL
    # window per runner — scoped to exactly the runners whose catalogs this
    # principal can read here. Anything else is unauthenticated, ungranted,
    # or unrelated driver work.
    await registry.ensure_fresh(
        runner_refs=catalog_refresh_refs(config, principal, driver_scope)
    )
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
