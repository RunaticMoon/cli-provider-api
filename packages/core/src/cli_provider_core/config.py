"""Strict operator configuration.

Operator config owns runner endpoints, allowed driver packages/versions, presets,
backend/model bindings, task policies, principals (API-key hashes) and workspaces.
Nothing here can be selected by an HTTP request.
"""

from __future__ import annotations

import json
import os
import re
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cli_provider_sdk import Alias, ID_PATTERN

_KEY_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limits(_Strict):
    max_body_bytes: int = Field(default=262_144, gt=0)
    max_events_per_run: int = Field(default=10_000, gt=0)
    max_events_returned: int = Field(default=1_000, gt=0)
    max_output_bytes: int = Field(default=1_048_576, gt=0)
    max_artifact_bytes: int = Field(default=1_048_576, gt=0)
    max_headers_bytes: int = Field(default=16_384, gt=0)


class Concurrency(_Strict):
    per_runner: int = Field(default=2, ge=1)
    per_principal: int = Field(default=2, ge=1)
    queue_timeout_seconds: float = Field(default=15.0, gt=0)
    # Bounded admission: outstanding (active + queued) runs beyond the active
    # capacity are refused before any task/attempt is allocated or dispatched.
    max_queued_per_runner: int = Field(default=4, ge=1)
    max_queued_per_principal: int = Field(default=4, ge=1)


class ApiSettings(_Strict):
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=0, le=65535)
    default_run_deadline_seconds: float = Field(default=60.0, gt=0)
    max_run_deadline_seconds: float = Field(default=120.0, gt=0)
    cancel_deadline_seconds: float = Field(default=5.0, gt=0)
    keepalive_seconds: float = Field(default=10.0, gt=0)
    # A single, fixed deadline for reading a whole request body. Byte caps alone
    # do not bound a slow drip feed; this is never renewed per chunk.
    request_body_timeout_seconds: float = Field(default=10.0, gt=0)
    limits: Limits = Field(default_factory=Limits)
    concurrency: Concurrency = Field(default_factory=Concurrency)


class RunnerConfig(_Strict):
    instance_id: str = Field(pattern=ID_PATTERN)
    driver_id: str = Field(pattern=ID_PATTERN)
    driver_version: str = Field(min_length=1)
    distribution: str = Field(min_length=1)
    socket_path: str = Field(min_length=1)
    enabled: bool = True
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    # Short timeout for control RPCs (manifest/probe/discover_models/cancel).
    control_timeout_seconds: float = Field(default=5.0, gt=0)
    # Optional operator override for the streaming run frame timeout. When unset
    # the controller derives it from the run's actual deadline + cancel budget.
    run_frame_timeout_seconds: float | None = Field(default=None, gt=0)


class PresetConfig(_Strict):
    alias: Alias
    runner_ref: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    task_policy: str = Field(default="text", min_length=1)
    enabled: bool = True
    stateless: bool = True
    # Explicit operator development opt-in: allows a *synthetic* driver's model
    # to be served while its verification status is unknown/not_run. It never
    # claims real verification and is refused for non-synthetic drivers.
    allow_synthetic_unverified: bool = False


class WorkspaceConfig(_Strict):
    workspace_id: str = Field(pattern=ID_PATTERN)
    description: str | None = None


class PrincipalConfig(_Strict):
    name: str = Field(pattern=ID_PATTERN)
    key_hash: str
    allowed_presets: list[Alias] = Field(min_length=1)
    allowed_workspaces: list[str] = Field(min_length=1)
    max_concurrency: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def _key_hash_is_a_hash(self) -> "PrincipalConfig":
        if _KEY_HASH_RE.match(self.key_hash) is None:
            raise ValueError(
                "key_hash must be an explicit 'sha256:<64 hex chars>' hash, "
                "never a plaintext key"
            )
        return self


class OperatorConfig(_Strict):
    schema_version: Literal[1]
    data_dir: str = Field(min_length=1)
    api: ApiSettings = Field(default_factory=ApiSettings)
    runners: list[RunnerConfig] = Field(min_length=1)
    presets: list[PresetConfig] = Field(min_length=1)
    workspaces: list[WorkspaceConfig] = Field(min_length=1)
    principals: list[PrincipalConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _cross_references(self) -> "OperatorConfig":
        runners = {r.instance_id for r in self.runners}
        if len(runners) != len(self.runners):
            raise ValueError("duplicate runner instance_id")

        presets = [p.alias for p in self.presets]
        if len(set(presets)) != len(presets):
            raise ValueError("duplicate preset alias")
        workspaces = {w.workspace_id for w in self.workspaces}
        if len(workspaces) != len(self.workspaces):
            raise ValueError("duplicate workspace_id")
        principals = [p.name for p in self.principals]
        if len(set(principals)) != len(principals):
            raise ValueError("duplicate principal name")

        for preset in self.presets:
            if preset.runner_ref not in runners:
                raise ValueError(
                    f"preset {preset.alias!r} references unknown runner "
                    f"{preset.runner_ref!r}"
                )
        preset_set = set(presets)
        for principal in self.principals:
            unknown = [p for p in principal.allowed_presets if p not in preset_set]
            if unknown:
                raise ValueError(
                    f"principal {principal.name!r} allows unknown presets {unknown!r}"
                )
            unknown_ws = [
                w for w in principal.allowed_workspaces if w not in workspaces
            ]
            if unknown_ws:
                raise ValueError(
                    f"principal {principal.name!r} allows unknown workspaces "
                    f"{unknown_ws!r}"
                )
        return self

    def runner_map(self) -> dict[str, RunnerConfig]:
        return {r.instance_id: r for r in self.runners}

    def preset_map(self) -> dict[str, PresetConfig]:
        return {p.alias: p for p in self.presets}

    def principal_map(self) -> dict[str, PrincipalConfig]:
        return {p.name: p for p in self.principals}

    def workspace_ids(self) -> set[str]:
        return {w.workspace_id for w in self.workspaces}

    def artifacts_dir(self) -> str:
        return os.path.join(self.data_dir, "artifacts")

    def db_path(self) -> str:
        return os.path.join(self.data_dir, "core.db")


def load_config(path: str) -> OperatorConfig:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read()
    if path.endswith(".json"):
        data = json.loads(raw)
    else:
        data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")
    return OperatorConfig.model_validate(data)
