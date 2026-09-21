"""GET /v1/models and the provider-scoped equivalent."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from cli_provider_core import (
    NotFound,
    OperatorConfig,
    PrincipalConfig,
    RunnerRegistry,
    dynamic_model_entries,
    models_refresh_refs,
)

from .auth import authenticate

router = APIRouter()


def _entries(
    config: OperatorConfig,
    registry: RunnerRegistry,
    principal: PrincipalConfig,
    driver_scope: str | None,
) -> list[dict[str, Any]]:
    runners = config.runner_map()
    entries: list[dict[str, Any]] = []
    for preset in config.presets:
        if not preset.enabled or preset.alias not in principal.allowed_presets:
            continue
        runner = runners[preset.runner_ref]
        if driver_scope is not None and runner.driver_id != driver_scope:
            continue
        if not registry.preset_available(preset.alias):
            continue
        health = registry.preset_health(preset.alias)
        entries.append(
            {
                "id": preset.alias,
                "object": "model",
                "created": 0,
                "owned_by": runner.driver_id,
                "root": preset.alias,
                "parent": None,
                "permission": [],
                # Truthful effective capabilities/provenance for this preset.
                "capabilities": health.capabilities if health else None,
                "verification": health.verification if health else None,
                "real_verification": bool(health and health.real_verification),
                "dynamic": False,
            }
        )
    # Verified+authorized dynamic aliases join the compatible list; the full
    # discovery view (incl. not-executable entries) is GET /api/v1/catalog.
    entries.extend(
        dynamic_model_entries(config, registry, principal, driver_scope)
    )
    return entries


def _assert_driver_scope_known(
    config: OperatorConfig, registry: RunnerRegistry, driver_scope: str
) -> None:
    if not any(r.driver_id == driver_scope for r in config.runners):
        raise NotFound("provider not found")


@router.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    principal = authenticate(request)
    config: OperatorConfig = request.app.state.config
    registry: RunnerRegistry = request.app.state.registry
    if config.catalogs:
        await registry.ensure_fresh(
            runner_refs=models_refresh_refs(config, principal, None)
        )
    return {"object": "list", "data": _entries(config, registry, principal, None)}


@router.get("/providers/{driver_id}/v1/models")
async def list_models_scoped(request: Request, driver_id: str) -> dict[str, Any]:
    principal = authenticate(request)
    config: OperatorConfig = request.app.state.config
    registry: RunnerRegistry = request.app.state.registry
    _assert_driver_scope_known(config, registry, driver_id)
    if config.catalogs:
        await registry.ensure_fresh(
            runner_refs=models_refresh_refs(config, principal, driver_id)
        )
    return {
        "object": "list",
        "data": _entries(config, registry, principal, driver_id),
    }
