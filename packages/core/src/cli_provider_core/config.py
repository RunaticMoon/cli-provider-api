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

from cli_provider_sdk import Alias, ID_PATTERN, validate_alias

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
    per_runner: int = Field(default=1, ge=1)
    # Global per-principal cap, enforced as min(this, principal.max_concurrency)
    # for both admission accounting and the in-flight semaphore.
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
    # Minimum interval between driver catalog refreshes triggered by read or
    # resolve paths. Refresh is singleflight: concurrent callers share one
    # pass, so a request burst cannot spawn a process-per-request storm.
    catalog_refresh_seconds: float = Field(default=30.0, gt=0)
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


class CatalogSourceConfig(_Strict):
    """Opt-in discovery source owned by the operator, not by requests.

    Models a runner's driver reports in its native catalog become visible as
    ``<alias_prefix><exact model_id>`` and — only when separately granted —
    runnable without a per-model preset. ``models=None`` is a provider-level
    grant over every verified+executable catalog entry; a list restricts the
    source to those exact ids. ``cost_tiers`` restricts admission to the
    descriptor's observed cost class (absent tier never matches).
    """

    name: str = Field(pattern=ID_PATTERN)
    runner_ref: str = Field(min_length=1)
    alias_prefix: str = Field(min_length=2, max_length=200)
    task_policy: str = Field(default="text", min_length=1)
    enabled: bool = True
    models: list[str] | None = Field(default=None, max_length=512)
    cost_tiers: list[str] | None = Field(default=None, max_length=16)
    # Same opt-in semantics as PresetConfig.allow_synthetic_unverified.
    allow_synthetic_unverified: bool = False

    @model_validator(mode="after")
    def _prefix_and_filters(self) -> "CatalogSourceConfig":
        if not self.alias_prefix.endswith("/"):
            raise ValueError("alias_prefix must end with '/'")
        try:
            validate_alias(self.alias_prefix + "x")
        except ValueError as exc:
            raise ValueError(f"alias_prefix {self.alias_prefix!r} is invalid: {exc}")
        if self.models is not None:
            for model in self.models:
                if re.match(ID_PATTERN, model) is None:
                    raise ValueError(f"catalog model {model!r} is not a model id")
            if len(set(self.models)) != len(self.models):
                raise ValueError("duplicate catalog model id")
        if self.cost_tiers is not None:
            for tier in self.cost_tiers:
                if not tier or len(tier) > 64:
                    raise ValueError("cost_tier entries must be bounded strings")
        return self


class WorkspaceConfig(_Strict):
    workspace_id: str = Field(pattern=ID_PATTERN)
    description: str | None = None


class PrincipalConfig(_Strict):
    name: str = Field(pattern=ID_PATTERN)
    key_hash: str
    # Static preset aliases AND exact concrete catalog aliases
    # ("<alias_prefix><model_id>") a principal may run.
    allowed_presets: list[Alias] = Field(min_length=1)
    # Read-only discovery grants: catalog sources visible in /api/v1/catalog.
    # A read-only grant never executes anything.
    allowed_catalogs: list[str] = Field(default_factory=list)
    # Execution grants: dynamic aliases admitted by these sources may run.
    # allowed_catalogs is metadata only; executable_catalogs is the run gate.
    executable_catalogs: list[str] = Field(default_factory=list)
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
    # Opt-in dynamic discovery sources. Empty means the deployment behaves
    # exactly like a static-presets-only configuration.
    catalogs: list[CatalogSourceConfig] = Field(default_factory=list)
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

        catalog_names = [c.name for c in self.catalogs]
        if len(set(catalog_names)) != len(catalog_names):
            raise ValueError("duplicate catalog source name")
        for source in self.catalogs:
            if source.runner_ref not in runners:
                raise ValueError(
                    f"catalog {source.name!r} references unknown runner "
                    f"{source.runner_ref!r}"
                )
        prefixes = [c.alias_prefix for c in self.catalogs if c.enabled]
        for index, prefix in enumerate(prefixes):
            for other in prefixes[index + 1 :]:
                if prefix.startswith(other) or other.startswith(prefix):
                    raise ValueError(
                        f"catalog alias prefixes {prefix!r} and {other!r} overlap"
                    )

        preset_set = set(presets)
        for principal in self.principals:
            unknown = [
                p
                for p in principal.allowed_presets
                if p not in preset_set and not self._catalog_alias(p)
            ]
            if unknown:
                raise ValueError(
                    f"principal {principal.name!r} allows unknown presets {unknown!r}"
                )
            for grant in principal.allowed_catalogs + principal.executable_catalogs:
                if grant not in catalog_names:
                    raise ValueError(
                        f"principal {principal.name!r} grants unknown catalog "
                        f"{grant!r}"
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

    def _catalog_alias(self, alias: str) -> bool:
        """A principal grant shaped like ``<source prefix><model id>``."""

        for source in self.catalogs:
            if not source.enabled or not alias.startswith(source.alias_prefix):
                continue
            tail = alias[len(source.alias_prefix) :]
            if re.match(ID_PATTERN, tail) is not None:
                return True
        return False

    def runner_map(self) -> dict[str, RunnerConfig]:
        return {r.instance_id: r for r in self.runners}

    def preset_map(self) -> dict[str, PresetConfig]:
        return {p.alias: p for p in self.presets}

    def catalog_map(self) -> dict[str, CatalogSourceConfig]:
        return {c.name: c for c in self.catalogs}

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
