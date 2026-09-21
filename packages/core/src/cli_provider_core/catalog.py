"""Dynamic catalog admission and model-alias resolution.

Discovery is never authorization. A ``CatalogSourceConfig`` names one runner
whose driver-reported catalog becomes addressable as ``<alias_prefix><exact
model_id>`` — no per-model preset required. Admission requires all of:

* a verified runner snapshot (``health.ok``; a failed/unknown/malformed
  catalog authorizes nothing),
* the descriptor's own verification status (``passed``, or the explicit
  synthetic-development opt-in),
* the driver's execution admission (``executable``),
* the source's operator filters (``models`` allowlist, ``cost_tiers``),

and, for execution, a principal grant: the source listed in
``executable_catalogs`` or the concrete alias listed in ``allowed_presets``.
``allowed_catalogs`` alone is a read-only metadata grant for the discovery
endpoint and can never run anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from cli_provider_sdk import (
    ID_PATTERN,
    EffortSupport,
    ModelDescriptor,
    VerificationStatus,
    resolve_effort,
)

from .config import (
    CatalogSourceConfig,
    OperatorConfig,
    PresetConfig,
    PrincipalConfig,
)
from .errors import (
    AuthorizationError,
    NotFound,
    RunnerUnavailable,
    UnsupportedCapability,
)
from .registry import RunnerHealth, RunnerRegistry

_ID = re.compile(ID_PATTERN)


@dataclass(frozen=True)
class ModelBinding:
    """The authorized model/effort binding for one admitted request."""

    preset: PresetConfig  # static preset, or a synthesized catalog binding
    requested_model_id: str
    resolved_model_id: str
    reasoning_effort: str | None
    effort_support: str | None
    dynamic: bool
    source: str | None

    def evidence(self) -> dict[str, Any]:
        """Durable run evidence persisted verbatim on the attempt."""
        return {
            "alias": self.preset.alias,
            "model_id": self.requested_model_id,
            "resolved_model": self.resolved_model_id,
            "reasoning_effort": self.reasoning_effort,
            "effort_support": self.effort_support,
            "dynamic": self.dynamic,
            "source": self.source,
        }


def principal_may_read(principal: PrincipalConfig, source: CatalogSourceConfig) -> bool:
    """Read-only metadata grant: catalog visibility, never execution."""
    return (
        source.name in principal.allowed_catalogs
        or source.name in principal.executable_catalogs
    )


def principal_may_execute(
    principal: PrincipalConfig, source: CatalogSourceConfig, alias: str
) -> bool:
    """Run gate: a source-wide execution grant or an exact approved alias."""
    return (
        source.name in principal.executable_catalogs
        or alias in principal.allowed_presets
    )


def source_admits(
    source: CatalogSourceConfig,
    descriptor: dict[str, Any],
    *,
    synthetic_runner: bool,
) -> tuple[bool, str | None]:
    """Operator-policy admission of one discovered descriptor."""
    if source.models is not None and descriptor["model_id"] not in source.models:
        return False, "not in the catalog source's approved model list"
    if source.cost_tiers is not None:
        if descriptor.get("cost_tier") not in source.cost_tiers:
            return False, "cost tier is not admitted by the catalog source"
    verification = descriptor.get("verification") or {}
    status = verification.get("status")
    if status == VerificationStatus.FAILED.value:
        return False, "model verification failed"
    if status != VerificationStatus.PASSED.value and not (
        synthetic_runner and source.allow_synthetic_unverified
    ):
        return False, f"model verification status {status!r} is not 'passed'"
    if descriptor.get("executable") is False:
        return False, "driver does not admit this model for execution"
    return True, None


def _descriptor(model: dict[str, Any] | None) -> ModelDescriptor | None:
    return model if model is None else ModelDescriptor.model_validate(model)


def _resolve_effort_target(
    *,
    descriptor: dict[str, Any] | None,
    effort: str | None,
    admit_target,
) -> tuple[str | None, str | None]:
    """Resolve an effort request; the target must be independently admitted.

    ``admit_target(model_id) -> (bool, reason)`` re-checks the resolved variant
    under the same authorization surface (static preset checks or the dynamic
    source policy) so an effort mapping can never hop to an unapproved model.
    """
    if effort is None:
        return (descriptor or {}).get("model_id"), None
    resolved, rejection = resolve_effort(_descriptor(descriptor), effort)
    if rejection is not None or resolved is None:
        return None, rejection or "effort could not be resolved"
    if resolved != (descriptor or {}).get("model_id"):
        admitted, reason = admit_target(resolved)
        if not admitted:
            return None, (
                f"effort resolves to {resolved!r}, which is not independently "
                f"authorized: {reason}"
            )
    return resolved, None


def resolve_model(
    *,
    config: OperatorConfig,
    registry: RunnerRegistry,
    principal: PrincipalConfig,
    alias: str,
    driver_scope: str | None = None,
    effort: str | None = None,
) -> ModelBinding:
    """Resolve a request alias to an authorized binding, static or dynamic."""
    preset = config.preset_map().get(alias)
    if preset is not None:
        return _resolve_static(
            config=config,
            registry=registry,
            principal=principal,
            preset=preset,
            driver_scope=driver_scope,
            effort=effort,
        )
    return _resolve_dynamic(
        config=config,
        registry=registry,
        principal=principal,
        alias=alias,
        driver_scope=driver_scope,
        effort=effort,
    )


def _resolve_static(
    *,
    config: OperatorConfig,
    registry: RunnerRegistry,
    principal: PrincipalConfig,
    preset: PresetConfig,
    driver_scope: str | None,
    effort: str | None,
) -> ModelBinding:
    if preset.alias not in principal.allowed_presets:
        raise AuthorizationError("principal is not allowed to use this model")
    runner = config.runner_map().get(preset.runner_ref)
    if runner is None:
        raise NotFound("model not found")
    if driver_scope is not None and runner.driver_id != driver_scope:
        raise NotFound("model not found")
    if not registry.preset_available(preset.alias):
        raise RunnerUnavailable(
            "model is not currently available (verification failed)"
        )
    descriptor = registry.model_descriptor(preset.runner_ref, preset.model_id)
    health = registry.runner_health(preset.runner_ref)

    def admit_target(model_id: str) -> tuple[bool, str | None]:
        target = registry.model_descriptor(preset.runner_ref, model_id)
        if target is None:
            return False, "variant is not present in the runner catalog"
        status = (target.get("verification") or {}).get("status")
        if status == VerificationStatus.FAILED.value:
            return False, "variant verification failed"
        if status != VerificationStatus.PASSED.value and not (
            health is not None
            and health.synthetic
            and preset.allow_synthetic_unverified
        ):
            return False, f"variant verification status {status!r} is not 'passed'"
        if target.get("executable") is False:
            return False, "driver does not admit the variant for execution"
        return True, None

    resolved, rejection = _resolve_effort_target(
        descriptor=descriptor,
        effort=effort,
        admit_target=admit_target,
    )
    if rejection is not None or resolved is None:
        raise UnsupportedCapability(
            rejection or "reasoning effort is not supported for this model"
        )
    support = None
    if effort is not None and descriptor is not None:
        support = ModelDescriptor.model_validate(descriptor).effort.value
    return ModelBinding(
        preset=preset,
        requested_model_id=preset.model_id,
        resolved_model_id=resolved,
        reasoning_effort=effort,
        effort_support=support,
        dynamic=False,
        source=None,
    )


def _resolve_dynamic(
    *,
    config: OperatorConfig,
    registry: RunnerRegistry,
    principal: PrincipalConfig,
    alias: str,
    driver_scope: str | None,
    effort: str | None,
) -> ModelBinding:
    for source in config.catalogs:
        if not source.enabled or not alias.startswith(source.alias_prefix):
            continue
        tail = alias[len(source.alias_prefix) :]
        # The tail must be one exact catalog id — never a wildcard, path, or
        # multi-segment expression that could bypass the admission checks.
        if not tail or _ID.match(tail) is None:
            continue
        runner = config.runner_map().get(source.runner_ref)
        if runner is None:
            continue
        if driver_scope is not None and runner.driver_id != driver_scope:
            continue
        # The principal run gate is checked before any catalog detail is
        # consulted, so an unauthorized caller cannot probe model existence.
        if not principal_may_execute(principal, source, alias):
            raise AuthorizationError(
                "principal is not allowed to use this model"
            )
        health = registry.runner_health(source.runner_ref)
        if health is None or not health.enabled or not health.ok:
            raise RunnerUnavailable(
                f"catalog source {source.name!r} is not currently available"
            )
        descriptor = health.model_descriptors.get(tail)
        if descriptor is None:
            raise NotFound("model not found")
        admitted, reason = source_admits(
            source, descriptor, synthetic_runner=health.synthetic
        )
        if not admitted:
            raise NotFound(
                f"model {tail!r} is not admitted by catalog source "
                f"{source.name!r}: {reason}"
            )

        def admit_target(model_id: str) -> tuple[bool, str | None]:
            target = health.model_descriptors.get(model_id)
            if target is None:
                return False, "variant is not present in the runner catalog"
            return source_admits(
                source, target, synthetic_runner=health.synthetic
            )

        resolved, rejection = _resolve_effort_target(
            descriptor=descriptor,
            effort=effort,
            admit_target=admit_target,
        )
        if rejection is not None or resolved is None:
            raise UnsupportedCapability(
                rejection or "reasoning effort is not supported for this model"
            )
        binding = PresetConfig(
            alias=alias,
            runner_ref=source.runner_ref,
            model_id=tail,
            task_policy=source.task_policy,
            allow_synthetic_unverified=source.allow_synthetic_unverified,
        )
        support = None
        if effort is not None:
            support = ModelDescriptor.model_validate(descriptor).effort.value
        return ModelBinding(
            preset=binding,
            requested_model_id=tail,
            resolved_model_id=resolved or tail,
            reasoning_effort=effort,
            effort_support=support,
            dynamic=True,
            source=source.name,
        )
    raise NotFound("model not found")


def catalog_view(
    config: OperatorConfig,
    registry: RunnerRegistry,
    principal: PrincipalConfig,
    driver_scope: str | None = None,
) -> list[dict[str, Any]]:
    """Authenticated discovery view, scoped to the principal's read grants."""
    entries: list[dict[str, Any]] = []
    runners = config.runner_map()
    for source in config.catalogs:
        if not source.enabled or not principal_may_read(principal, source):
            continue
        runner = runners.get(source.runner_ref)
        if runner is None:
            continue
        if driver_scope is not None and runner.driver_id != driver_scope:
            continue
        health = registry.runner_health(source.runner_ref)
        entries.append(
            _source_view(source, runner, health, registry, principal)
        )
    return entries


def _source_view(
    source: CatalogSourceConfig,
    runner: Any,
    health: RunnerHealth | None,
    registry: RunnerRegistry,
    principal: PrincipalConfig,
) -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    descriptors = health.model_descriptors if health is not None else {}
    runner_ok = bool(health and health.enabled and health.ok)
    for model_id in sorted(descriptors):
        descriptor = descriptors[model_id]
        alias = source.alias_prefix + model_id
        admitted, reason = (
            source_admits(
                source, descriptor, synthetic_runner=health.synthetic
            )
            if health is not None
            else (False, "runner not verified")
        )
        executable = bool(
            admitted
            and runner_ok
            and principal_may_execute(principal, source, alias)
        )
        entry = dict(descriptor)
        # The driver-reported admission is kept distinct from the endpoint's
        # principal-scoped execution decision.
        entry["driver_executable"] = descriptor.get("executable", True)
        entry["alias"] = alias
        entry["admitted"] = admitted
        entry["executable"] = executable
        if executable:
            entry["rejection"] = None
        elif not admitted:
            entry["rejection"] = reason
        elif not runner_ok:
            entry["rejection"] = "runner catalog is not verified/available"
        else:
            entry["rejection"] = "principal lacks an execution grant"
        models.append(entry)
    return {
        "object": "catalog",
        "source": source.name,
        "instance_id": runner.instance_id,
        "driver_id": runner.driver_id,
        "ok": bool(health and health.ok),
        "detail": health.detail if health is not None else "not checked",
        "cli_version": (health.probe or {}).get("cli_version")
        if health is not None
        else None,
        "observed_at": health.observed_at if health is not None else None,
        "stale": registry.catalog_stale(runner.instance_id),
        "refresh_seconds": registry.catalog_refresh_seconds,
        "models": models,
    }


def dynamic_model_entries(
    config: OperatorConfig,
    registry: RunnerRegistry,
    principal: PrincipalConfig,
    driver_scope: str | None = None,
) -> list[dict[str, Any]]:
    """Admitted+executable dynamic aliases for the compatible /v1/models list.

    Only models this principal may actually execute appear here; the full
    discovery view (including not-admitted entries) lives on the catalog
    endpoint.
    """
    runners = config.runner_map()
    entries: list[dict[str, Any]] = []
    static_aliases = set(config.preset_map())
    for source in config.catalogs:
        if not source.enabled:
            continue
        runner = runners.get(source.runner_ref)
        if runner is None:
            continue
        if driver_scope is not None and runner.driver_id != driver_scope:
            continue
        health = registry.runner_health(source.runner_ref)
        if health is None or not health.enabled or not health.ok:
            continue
        for model_id in sorted(health.model_descriptors):
            alias = source.alias_prefix + model_id
            if alias in static_aliases:
                # A static preset with the same alias wins; never duplicate.
                continue
            if not principal_may_execute(principal, source, alias):
                continue
            descriptor = health.model_descriptors[model_id]
            admitted, _reason = source_admits(
                source, descriptor, synthetic_runner=health.synthetic
            )
            if not admitted:
                continue
            verification = descriptor.get("verification") or {}
            passed = verification.get("status") == VerificationStatus.PASSED.value
            capabilities = dict(health.capabilities or {})
            capabilities["supported_transports"] = (health.manifest or {}).get(
                "supported_transports", []
            )
            capabilities["task_policy"] = source.task_policy
            capabilities["synthetic"] = health.synthetic
            capabilities["real_verification"] = bool(
                passed and not health.synthetic
            )
            entries.append(
                {
                    "id": alias,
                    "object": "model",
                    "created": 0,
                    "owned_by": runner.driver_id,
                    "root": alias,
                    "parent": None,
                    "permission": [],
                    "capabilities": capabilities,
                    "verification": verification,
                    "real_verification": bool(
                        passed and not health.synthetic
                    ),
                    "model_id": model_id,
                    "effort": descriptor.get("effort", EffortSupport.UNKNOWN.value),
                    "effort_options": descriptor.get("effort_options") or [],
                    "effort_variants": descriptor.get("effort_variants") or {},
                    "cost_tier": descriptor.get("cost_tier"),
                    "family": descriptor.get("family"),
                    "dynamic": True,
                    "catalog": source.name,
                }
            )
    return entries
