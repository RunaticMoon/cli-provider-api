"""Runner registry: verify runners/presets over UDS without loading drivers.

Enabled is not enough. A runner must present a manifest/probe/model list that
validates against the SDK schemas, declare the expected driver ID and version,
report a probe OK and expose the preset's bound model with an acceptable
verification status. The registry only ever speaks the Runner RPC; it never
calls ``entry_point.load()`` and never imports a driver package.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

from cli_provider_runner.protocol import RunnerRuntime

from .config import OperatorConfig, PresetConfig, RunnerConfig
from .runner import RunnerSession, UdsRunnerSession

# Conservative fallback for a Runner that does not declare its own cancel
# cleanup budget. It matches the Runner server's default cancel deadline (5 s)
# worst case (two bounded cancel waits), so an undeclared budget never
# under-bounds the outer run budget.
_FALLBACK_CANCEL_CLEANUP_SECONDS = 10.0


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
    # Runner-owned runtime capacity (verified over the `runtime` RPC). A runner
    # that does not declare it is treated conservatively as serial (1).
    max_parallel_runs: int = 1
    max_queue: int = 1
    # Runner-owned bounded cancellation cleanup budget, also verified over the
    # `runtime` RPC. The controller derives its finite outer run budget from it.
    cancel_cleanup_seconds: float = _FALLBACK_CANCEL_CLEANUP_SECONDS
    # Last successful verify timestamp (wall ISO + monotonic): the catalog
    # snapshot's provenance and the basis of staleness reporting. A failed
    # refresh keeps the previous snapshot but leaves ``ok`` False.
    observed_at: str | None = None
    observed_monotonic: float | None = None


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
        self._refresh_guard = asyncio.Lock()
        # Per-runner last-refresh-attempt marks: a scoped refresh must never
        # mark an untouched runner fresh, and one runner's TTL is independent
        # of another's.
        self._fresh_marks: dict[str, float] = {}

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

    def runner_capacity(self, instance_id: str) -> int:
        """Verified max concurrent runs for a runner (conservatively 1)."""
        health = self._runners.get(instance_id)
        if health is None or not health.ok:
            return 1
        return max(1, health.max_parallel_runs)

    def runner_cleanup_seconds(self, instance_id: str) -> float:
        """Verified Runner cancel/cleanup budget for the outer run bound.

        A runner that did not report or verify it falls back to a conservative,
        finite value instead of the API guessing an unbounded budget.
        """
        health = self._runners.get(instance_id)
        if health is None or not health.ok:
            return _FALLBACK_CANCEL_CLEANUP_SECONDS
        return health.cancel_cleanup_seconds

    def runner_synthetic(self, instance_id: str) -> bool:
        """Verified Runner manifest provenance, for persisting on an attempt.

        An unverified/unknown runner is treated as synthetic so a run is never
        mislabelled as real (native) evidence it cannot substantiate.
        """
        health = self._runners.get(instance_id)
        if health is None or not health.ok:
            return True
        return health.synthetic

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

    @property
    def catalog_refresh_seconds(self) -> float:
        return self._config.api.catalog_refresh_seconds

    def catalog_stale(self, instance_id: str) -> bool:
        """True when the runner's catalog snapshot is absent, failed, or older
        than the configured refresh interval."""
        health = self._runners.get(instance_id)
        if health is None or health.observed_monotonic is None:
            return True
        ttl = self._config.api.catalog_refresh_seconds
        return (time.monotonic() - health.observed_monotonic) > ttl

    def model_descriptor(
        self, instance_id: str, model_id: str
    ) -> dict[str, Any] | None:
        health = self._runners.get(instance_id)
        if health is None:
            return None
        return health.model_descriptors.get(model_id)

    # ------------------------------------------------------------ verifying

    async def refresh(
        self, runner_refs: set[str] | None = None
    ) -> dict[str, RunnerHealth]:
        """Probe runners and verify preset model bindings.

        ``runner_refs=None`` (the startup path) verifies every configured
        runner; a set restricts the pass to those instance ids — untouched
        runners keep their snapshot and freshness mark. Each attempted
        runner gets its own mark, so a scoped pass can never refresh-stamp
        a runner it did not contact.
        """
        async with self._lock:
            for runner in self._config.runners:
                if runner_refs is not None and runner.instance_id not in runner_refs:
                    continue
                health = self._runners[runner.instance_id]
                if not runner.enabled:
                    health.ok = False
                    health.detail = "disabled by operator config"
                else:
                    await self._verify_runner(runner, health)
                # The mark records when this runner's attempt FINISHED. A
                # slow verify that outlasts the TTL must still count as one
                # completed attempt for concurrent waiters — marking before
                # the attempt would look stale the moment the guard opens.
                self._fresh_marks[runner.instance_id] = time.monotonic()
            # Preset health is derived runner state: re-derive it only for
            # presets bound to runners this pass touched, so a scoped
            # refresh never mutates a skipped runner's derived presets.
            for preset in self._config.presets:
                if runner_refs is not None and preset.runner_ref not in runner_refs:
                    continue
                self._verify_preset(preset)
        return self._runners

    def _fresh_within_ttl(self, instance_id: str) -> bool:
        mark = self._fresh_marks.get(instance_id)
        ttl = self._config.api.catalog_refresh_seconds
        return mark is not None and (time.monotonic() - mark) < ttl

    async def ensure_fresh(
        self, runner_refs: set[str] | None = None, *, force: bool = False
    ) -> None:
        """Bounded singleflight TTL refresh for catalog-aware read paths.

        ``runner_refs`` scopes the pass: only those runners are probed, so a
        narrowly-authorized request can never trigger discovery RPCs on
        unrelated runners. Per runner, at most one pass runs per
        ``api.catalog_refresh_seconds`` interval; concurrent callers queue on
        the guard and re-check their own stale set, so a burst of reads can
        never spawn a per-request discovery storm.
        """
        refs = (
            {r.instance_id for r in self._config.runners}
            if runner_refs is None
            else set(runner_refs)
        )
        if not force and refs and all(
            self._fresh_within_ttl(ref) for ref in refs
        ):
            return
        async with self._refresh_guard:
            targets = (
                refs
                if force
                else {ref for ref in refs if not self._fresh_within_ttl(ref)}
            )
            if targets:
                await self.refresh(targets)

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
                if descriptor.model_id in descriptors:
                    health.ok = False
                    health.detail = (
                        f"catalog reports duplicate model_id "
                        f"{descriptor.model_id!r}; refusing to pick a row"
                    )
                    return
                descriptors[descriptor.model_id] = descriptor.model_dump(mode="json")

            runtime = await self._runtime_capacity(session, health)
            if runtime is None:
                return

            health.driver_id = manifest.driver_id
            health.driver_version = manifest.version
            health.manifest = manifest.model_dump(mode="json")
            health.probe = probe.model_dump(mode="json")
            health.capabilities = probe.capabilities.model_dump(mode="json")
            health.synthetic = bool(manifest.synthetic)
            health.model_descriptors = descriptors
            health.models = sorted(descriptors)
            health.max_parallel_runs = runtime["max_parallel_runs"]
            health.max_queue = runtime["max_queue"]
            health.cancel_cleanup_seconds = runtime["cancel_cleanup_seconds"]
            health.observed_at = datetime.now(timezone.utc).isoformat()
            health.observed_monotonic = time.monotonic()
            health.ok = True
            health.detail = "manifest/probe/discover verified against the SDK schemas"
        except Exception as exc:  # noqa: BLE001 - classified for health output
            health.ok = False
            health.detail = f"verification failed: {type(exc).__name__}"
        finally:
            await session.aclose()

    async def _runtime_capacity(
        self, session: RunnerSession, health: RunnerHealth
    ) -> dict[str, int] | None:
        """Query the Runner's own runtime capacity (verified, never guessed).

        A session that cannot report it is treated conservatively as serial. A
        session that reports an invalid payload fails verification rather than
        letting the API dispatch work the Runner cannot run.
        """
        method = getattr(session, "runtime", None)
        if not callable(method):
            # Validate the conservative fallback through the same schema as the
            # reported path so both agree (max_queue >= 1, bounded cleanup).
            fallback = RunnerRuntime(
                max_parallel_runs=1,
                max_queue=1,
                cancel_cleanup_seconds=_FALLBACK_CANCEL_CLEANUP_SECONDS,
            )
            return fallback.model_dump(mode="json")
        try:
            raw = await method()
        except Exception:  # noqa: BLE001 - classified for health output
            health.ok = False
            health.detail = "runner runtime capability query failed"
            return None
        try:
            return RunnerRuntime.model_validate(raw).model_dump(mode="json")
        except ValidationError:
            health.ok = False
            health.detail = "runner runtime capability failed SDK schema validation"
            return None

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
        passed = status == VerificationStatus.PASSED.value
        failed = status == VerificationStatus.FAILED.value
        # A synthetic driver can never claim real verification: its self-reported
        # status is only ever trusted as a development opt-in, and that opt-in
        # covers only unknown/not_run. An explicit `failed` probe is refused
        # unconditionally.
        health.real_verification = bool(passed and not runner.synthetic)
        if failed:
            health.verified = False
            health.detail = (
                f"model {preset.model_id!r} verification failed (source "
                f"{verification.get('source')!r}); failed status is never overridden"
            )
            return
        if not passed and not (runner.synthetic and preset.allow_synthetic_unverified):
            health.verified = False
            health.detail = (
                f"model {preset.model_id!r} verification status {status!r} is not "
                f"'passed' (source {verification.get('source')!r})"
            )
            return
        if descriptor.get("executable") is False:
            # Catalog membership is verified but the driver's own execution
            # policy (operator allowlist / prepared lane) does not admit this
            # model. Discovery is not authorization: the preset stays
            # unavailable instead of degrading to a driver-side failure.
            health.verified = False
            health.detail = (
                f"model {preset.model_id!r} is catalog-verified but the driver "
                "does not admit it for execution"
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
                "max_parallel_runs": health.max_parallel_runs,
                "max_queue": health.max_queue,
                "cancel_cleanup_seconds": health.cancel_cleanup_seconds,
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
