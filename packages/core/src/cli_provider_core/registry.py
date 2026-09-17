"""Runner registry: verify runners/presets over UDS without loading drivers.

Enabled is not enough. A runner must present a manifest/probe/model list that
validates against the SDK schemas, declare the expected driver ID and version,
report a probe OK and expose the preset's bound model with an acceptable
verification status. The registry only ever speaks the Runner RPC; it never
calls ``entry_point.load()`` and never imports a driver package.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from cli_provider_sdk import (
    SDK_VERSION,
    DriverManifest,
    ModelDescriptor,
    ProbeReport,
    RoleMode,
    StreamingMode,
    VerificationStatus,
)
from pydantic import ValidationError

from .config import OperatorConfig, PresetConfig, RunnerConfig
from .runner import RunnerSession, UdsRunnerSession


@dataclass
class RunnerHealth:
    instance_id: str
    enabled: bool
    ok: bool = False
    detail: str = "not checked"
    driver_id: str | None = None
    driver_version: str | None = None
    models: list[str] = field(default_factory=list)
    model_descriptors: dict[str, dict[str, Any]] = field(default_factory=dict)
    capabilities: dict[str, Any] | None = None
    synthetic: bool = False
    probe: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None


@dataclass
class PresetHealth:
    alias: str
    runner_ref: str
    model_id: str
    enabled: bool
    verified: bool = False
    detail: str = "not checked"
    verification: dict[str, Any] | None = None
    capabilities: dict[str, Any] | None = None
    synthetic: bool = False
    real_verification: bool = False


SessionFactory = Callable[[RunnerConfig], RunnerSession]


def default_session_factory(config: RunnerConfig) -> RunnerSession:
    return UdsRunnerSession(
        config.socket_path,
        connect_timeout_seconds=config.connect_timeout_seconds,
        call_timeout_seconds=config.control_timeout_seconds,
        run_timeout_seconds=config.run_frame_timeout_seconds,
    )


class RunnerRegistry:
    def __init__(
        self,
        config: OperatorConfig,
        *,
        session_factory: SessionFactory = default_session_factory,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._runners: dict[str, RunnerHealth] = {
            r.instance_id: RunnerHealth(
                instance_id=r.instance_id, enabled=r.enabled, detail="not checked"
            )
            for r in config.runners
        }
        self._presets: dict[str, PresetHealth] = {
            p.alias: PresetHealth(
                alias=p.alias,
                runner_ref=p.runner_ref,
                model_id=p.model_id,
                enabled=p.enabled,
                detail="not checked",
            )
            for p in config.presets
        }
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- snapshot

    def runner_health(self, instance_id: str) -> RunnerHealth | None:
        return self._runners.get(instance_id)

    def preset_health(self, alias: str) -> PresetHealth | None:
        return self._presets.get(alias)

    def runner_config(self, instance_id: str) -> RunnerConfig:
        return self._config.runner_map()[instance_id]

    def preset_config(self, alias: str) -> PresetConfig | None:
        return self._config.preset_map().get(alias)

    def session(self, instance_id: str) -> RunnerSession:
        return self._session_factory(self.runner_config(instance_id))

    def runner_available(self, instance_id: str) -> bool:
        health = self._runners.get(instance_id)
        return bool(health and health.enabled and health.ok)

    def preset_available(self, alias: str) -> bool:
        preset = self._presets.get(alias)
        if preset is None or not preset.enabled or not preset.verified:
            return False
        return self.runner_available(preset.runner_ref)

    def preset_capabilities(self, alias: str) -> dict[str, Any] | None:
        preset = self._presets.get(alias)
        return preset.capabilities if preset else None

    def enabled_preset_aliases(self) -> list[str]:
        return [
            alias
            for alias, preset in self._presets.items()
            if preset.enabled and preset.verified
        ]

    # ------------------------------------------------------------ verifying

    async def refresh(self) -> dict[str, RunnerHealth]:
        """Probe every configured runner and verify preset model bindings."""
        async with self._lock:
            for runner in self._config.runners:
                health = self._runners[runner.instance_id]
                if not runner.enabled:
                    health.ok = False
                    health.detail = "disabled by operator config"
                    continue
                await self._verify_runner(runner, health)
            for preset in self._config.presets:
                self._verify_preset(preset)
        return self._runners

    async def _verify_runner(self, runner: RunnerConfig, health: RunnerHealth) -> None:
        session = self._session_factory(runner)
        try:
            manifest = await self._validated(
                DriverManifest, await session.manifest(), health, "manifest"
            )
            if manifest is None:
                return
            if manifest.driver_id != runner.driver_id:
                health.ok = False
                health.detail = (
                    f"manifest driver_id {manifest.driver_id!r} != configured "
                    f"{runner.driver_id!r}"
                )
                return
            if manifest.version != runner.driver_version:
                health.ok = False
                health.detail = (
                    f"manifest version {manifest.version!r} != configured "
                    f"{runner.driver_version!r}"
                )
                return
            if manifest.sdk_version != SDK_VERSION:
                health.ok = False
                health.detail = (
                    f"manifest sdk_version {manifest.sdk_version!r} != supported "
                    f"{SDK_VERSION!r}"
                )
                return

            probe = await self._validated(
                ProbeReport, await session.probe(), health, "probe"
            )
            if probe is None:
                return
            if not probe.ok:
                health.ok = False
                health.detail = "probe did not report ok"
                return

            raw_models = await session.discover_models()
            descriptors: dict[str, dict[str, Any]] = {}
            for raw in raw_models:
                descriptor = await self._validated(
                    ModelDescriptor, raw, health, "model descriptor"
                )
                if descriptor is None:
                    return
                descriptors[descriptor.model_id] = descriptor.model_dump(mode="json")

            health.driver_id = manifest.driver_id
            health.driver_version = manifest.version
            health.manifest = manifest.model_dump(mode="json")
            health.probe = probe.model_dump(mode="json")
            health.capabilities = probe.capabilities.model_dump(mode="json")
            health.synthetic = bool(manifest.synthetic)
            health.model_descriptors = descriptors
            health.models = sorted(descriptors)
            health.ok = True
            health.detail = "manifest/probe/discover verified against the SDK schemas"
        except Exception as exc:  # noqa: BLE001 - classified for health output
            health.ok = False
            health.detail = f"verification failed: {type(exc).__name__}"
        finally:
            await session.aclose()

    async def _validated(
        self,
        model: Any,
        payload: Any,
        health: RunnerHealth,
        label: str,
    ) -> Any | None:
        try:
            return (
                payload
                if isinstance(payload, model)
                else model.model_validate(payload)
            )
        except ValidationError:
            health.ok = False
            health.detail = f"{label} failed SDK schema validation"
            return None

    def _verify_preset(self, preset: PresetConfig) -> None:
        health = self._presets[preset.alias]
        health.enabled = preset.enabled
        if not preset.enabled:
            health.verified = False
            health.detail = "disabled by operator config"
            return
        runner = self._runners.get(preset.runner_ref)
        if runner is None or not runner.ok:
            health.verified = False
            health.detail = f"runner {preset.runner_ref!r} not verified"
            return
        descriptor = runner.model_descriptors.get(preset.model_id)
        if descriptor is None:
            health.verified = False
            health.detail = (
                f"model {preset.model_id!r} not present in discovered models"
            )
            return

        verification = descriptor.get("verification") or {}
        status = verification.get("status")
        health.verification = verification
        health.synthetic = runner.synthetic
        if status == VerificationStatus.PASSED.value:
            health.real_verification = True
        elif runner.synthetic and preset.allow_synthetic_unverified:
            # Explicit operator development opt-in: the synthetic driver's model
            # may be served without claiming real verification.
            health.real_verification = False
        else:
            health.verified = False
            health.detail = (
                f"model {preset.model_id!r} verification status {status!r} is not "
                f"'passed' (source {verification.get('source')!r})"
            )
            return

        capabilities = runner.capabilities or {}
        if capabilities.get("streaming") == StreamingMode.NONE.value:
            health.verified = False
            health.detail = "driver declares streaming=none; text chat is unavailable"
            return
        if capabilities.get("roles") == RoleMode.UNSUPPORTED.value:
            health.verified = False
            health.detail = "driver declares roles=unsupported"
            return

        health.capabilities = {
            **capabilities,
            "supported_transports": (runner.manifest or {}).get(
                "supported_transports", []
            ),
            "task_policy": preset.task_policy,
            "synthetic": runner.synthetic,
            "real_verification": health.real_verification,
        }
        health.verified = True
        if health.real_verification:
            health.detail = "model binding verified"
        else:
            health.detail = (
                "synthetic development opt-in; model verification is not real"
            )

    def ready(self) -> tuple[bool, dict[str, Any]]:
        detail: dict[str, Any] = {"runners": {}, "presets": {}}
        ready = True
        for instance_id, health in self._runners.items():
            detail["runners"][instance_id] = {
                "ok": health.ok,
                "detail": health.detail,
                "synthetic": health.synthetic,
                "capabilities": health.capabilities,
            }
            if health.enabled and not health.ok:
                ready = False
        for alias, preset in self._presets.items():
            detail["presets"][alias] = {
                "verified": preset.verified,
                "real_verification": preset.real_verification,
                "synthetic": preset.synthetic,
                "detail": preset.detail,
            }
            if preset.enabled and not preset.verified:
                ready = False
        return ready, detail

    def ready_status(self) -> bool:
        """Minimal readiness without building the internal detail map."""
        for health in self._runners.values():
            if health.enabled and not health.ok:
                return False
        for preset in self._presets.values():
            if preset.enabled and not preset.verified:
                return False
        return True
