"""API-key authentication and preset/workspace scoping."""

from __future__ import annotations

from fastapi import Request

from cli_provider_core import (
    AuthenticationError,
    AuthorizationError,
    NotFound,
    OperatorConfig,
    PrincipalConfig,
    RunnerRegistry,
    RunnerUnavailable,
    verify_api_key,
)


def authenticate(request: Request) -> PrincipalConfig:
    app = request.app
    config: OperatorConfig = app.state.config
    header = request.headers.get("authorization")
    if not header or not header.lower().startswith("bearer "):
        raise AuthenticationError("missing bearer API key")
    key = header[7:].strip()
    if not key:
        raise AuthenticationError("missing bearer API key")
    for principal in config.principals:
        if verify_api_key(key, principal.key_hash):
            request.state.principal = principal
            return principal
    raise AuthenticationError("invalid API key")


def current_principal(request: Request) -> PrincipalConfig:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return authenticate(request)
    return principal


def resolve_preset(
    *,
    config: OperatorConfig,
    registry: RunnerRegistry,
    principal: PrincipalConfig,
    alias: str,
    driver_scope: str | None = None,
):
    preset = config.preset_map().get(alias)
    if preset is None:
        raise NotFound("model not found")
    if alias not in principal.allowed_presets:
        raise AuthorizationError("principal is not allowed to use this model")
    runner = config.runner_map().get(preset.runner_ref)
    if runner is None:
        raise NotFound("model not found")
    if driver_scope is not None and runner.driver_id != driver_scope:
        raise NotFound("model not found")
    if not registry.preset_available(alias):
        raise RunnerUnavailable("model is not currently available (verification failed)")
    return preset, runner


def check_workspace(
    *, config: OperatorConfig, principal: PrincipalConfig, workspace_id: str
) -> None:
    if workspace_id not in config.workspace_ids():
        raise NotFound("workspace not found")
    if workspace_id not in principal.allowed_workspaces:
        raise AuthorizationError("principal is not allowed to use this workspace")
