"""Synthetic mock ProviderDriver.

This driver performs genuine asynchronous local fixture behaviour. It never
calls a real CLI, provider or network. ALL of its evidence is synthetic and is
labelled as such on every event; it must never be treated as a provider run.

Behaviour is selected by the OPERATOR at Runner launch (environment), never by
an HTTP/run request:
  - CLI_DRIVER_MOCK_BEHAVIOR:
      success | partial | failed | hang | hang_ignores_cancel | crash |
      malformed | slow | many_events
  - CLI_DRIVER_MOCK_MALFORMED_MODE:
      duplicate_sequence | event_after_terminal | no_terminal
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import AsyncIterator

from cli_provider_sdk import (
    BaseDriver,
    CancelResult,
    Capabilities,
    DriverError,
    DriverManifest,
    MessageDeltaEvent,
    MessageDeltaPayload,
    ModelDescriptor,
    NormalizedRequest,
    ProbeReport,
    RoleMode,
    RunCancelledEvent,
    RunCancelledPayload,
    RunCompletedEvent,
    RunCompletedPayload,
    RunEvent,
    RunFailedEvent,
    RunFailedPayload,
    RunStartedEvent,
    RunStartedPayload,
    RuntimeContext,
    resolve_effort,
    SDK_VERSION,
    SessionMode,
    StreamingMode,
    StructuredOutputMode,
    TransportKind,
    Usage,
    UsageProvenance,
    Verification,
    VerificationStatus,
)

BEHAVIOR_ENV = "CLI_DRIVER_MOCK_BEHAVIOR"
CANCEL_DETAIL_ENV = "CLI_DRIVER_MOCK_CANCEL_DETAIL"
MALFORMED_ENV = "CLI_DRIVER_MOCK_MALFORMED_MODE"
# Optional JSON catalog fixture ({"models": [{model_id, display_name?,
# verification?, effort?, effort_options?, effort_variants?, cost_tier?,
# family?, aliases?, executable?}]} or a bare list). Re-read on every
# discover_models call so tests exercise add/remove/refresh without a restart.
CATALOG_ENV = "CLI_DRIVER_MOCK_CATALOG_FILE"

SYNTHETIC_SOURCE = "mock-fixture"
SLOW_DELTAS = 30
SLOW_DELAY_SECONDS = 0.2
# Far beyond any small in-memory fan-out queue, to prove event delivery cannot
# block runtime progress.
MANY_DELTAS = 300


class MockBehavior(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    HANG = "hang"
    HANG_IGNORES_CANCEL = "hang_ignores_cancel"
    CRASH = "crash"
    MALFORMED = "malformed"
    SLOW = "slow"
    MANY_EVENTS = "many_events"


class MockMalformedMode(str, Enum):
    DUPLICATE_SEQUENCE = "duplicate_sequence"
    EVENT_AFTER_TERMINAL = "event_after_terminal"
    NO_TERMINAL = "no_terminal"


def _resolve(enum_type, raw: str | None, default):
    if raw is None:
        return default
    try:
        return enum_type(raw)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_type)
        raise ValueError(f"invalid {enum_type.__name__} {raw!r}; allowed: {allowed}") from exc


class MockDriver(BaseDriver):
    def __init__(
        self,
        behavior: MockBehavior | str | None = None,
        malformed_mode: MockMalformedMode | str | None = None,
    ) -> None:
        self._behavior = _resolve(
            MockBehavior,
            behavior if behavior is not None else os.environ.get(BEHAVIOR_ENV),
            MockBehavior.SUCCESS,
        )
        self._malformed_mode = _resolve(
            MockMalformedMode,
            malformed_mode
            if malformed_mode is not None
            else os.environ.get(MALFORMED_ENV),
            MockMalformedMode.DUPLICATE_SEQUENCE,
        )
        # Test-only knob: a long driver detail lets the Runner's truncation of
        # the driver-supplied deadline detail be exercised.
        self._cancel_detail = os.environ.get(CANCEL_DETAIL_ENV) or None
        self._cancelled = asyncio.Event()
        self._closed = False

    @property
    def behavior(self) -> MockBehavior:
        return self._behavior

    @property
    def manifest(self) -> DriverManifest:
        return DriverManifest(
            driver_id="mock",
            name="Mock Driver (synthetic fixtures)",
            version="0.1.0",
            sdk_version=SDK_VERSION,
            protocol_family="mock",
            supported_transports=[TransportKind.STDIO],
            synthetic=True,
        )

    def _capabilities(self) -> Capabilities:
        return Capabilities(
            streaming=StreamingMode.NATIVE,
            sessions=SessionMode.NONE,
            roles=RoleMode.SERIALIZED,
            structured_output=StructuredOutputMode.NONE,
            external_tool_calls=False,
            internal_tools=False,
            vision=False,
            workspace_write=False,
            web_search=False,
            usage=UsageProvenance.UNKNOWN,
        )

    async def probe(self, ctx: RuntimeContext) -> ProbeReport:
        return ProbeReport(
            ok=True,
            driver_id=self.manifest.driver_id,
            driver_version=self.manifest.version,
            cli_version=None,
            capabilities=self._capabilities(),
            notes=[
                "synthetic mock driver: no real CLI, provider or network call",
                "all evidence is labelled synthetic",
            ],
        )

    def _catalog(self) -> list[ModelDescriptor]:
        path = os.environ.get(CATALOG_ENV)
        if not path:
            return [
                ModelDescriptor(
                    model_id="mock-model",
                    display_name="Mock Model (synthetic)",
                    verification=Verification(
                        status=VerificationStatus.UNKNOWN,
                        source=SYNTHETIC_SOURCE,
                        reason="synthetic fixture; no real CLI verification performed",
                    ),
                )
            ]
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A malformed catalog must fail discovery, never authorize models.
            raise DriverError(
                f"mock catalog file unreadable: {type(exc).__name__}"
            ) from exc
        rows = data.get("models") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise DriverError("mock catalog must be a list or {'models': [...]}")
        descriptors: list[ModelDescriptor] = []
        for raw in rows:
            if not isinstance(raw, dict) or not isinstance(raw.get("model_id"), str):
                raise DriverError("mock catalog row is not an object with model_id")
            verification = raw.get("verification")
            if verification is None:
                verification = {
                    "status": raw.get("verification_status", "unknown"),
                    "source": SYNTHETIC_SOURCE,
                    "reason": "synthetic fixture; no real CLI verification performed",
                }
            descriptors.append(
                ModelDescriptor(
                    model_id=raw["model_id"],
                    display_name=raw.get("display_name") or raw["model_id"],
                    verification=Verification.model_validate(verification),
                    effort=raw.get("effort", "unknown"),
                    effort_options=list(raw.get("effort_options") or []),
                    effort_variants=dict(raw.get("effort_variants") or {}),
                    cost_tier=raw.get("cost_tier"),
                    family=raw.get("family"),
                    aliases=list(raw.get("aliases") or []),
                    executable=bool(raw.get("executable", True)),
                )
            )
        return descriptors

    async def discover_models(self, ctx: RuntimeContext) -> list[ModelDescriptor]:
        return self._catalog()

    def _resolve_run_model(
        self, request: NormalizedRequest
    ) -> tuple[str | None, tuple[str, str] | None]:
        """Resolve the executed model id + verify effort against the catalog.

        Mirrors the native drivers: the wire carries the admitted/requested id
        plus the effort token; the driver re-derives the exact target from its
        own catalog and refuses a ``resolved_model`` that disagrees.
        """
        effort = request.reasoning_effort
        if effort is None:
            # No effort resolution happened, so there is no variant evidence to
            # report — ``resolved_model`` stays unset (the executed model is
            # simply the admitted ``model_alias``).
            return None, None
        try:
            descriptors = self._catalog()
        except DriverError:
            return None, (
                "catalog_unavailable",
                "the mock catalog could not be read; refusing effort resolution",
            )
        matches = [d for d in descriptors if d.model_id == request.model_alias]
        descriptor = matches[0] if len(matches) == 1 else None
        resolved, rejection = resolve_effort(descriptor, effort)
        if rejection is not None or resolved is None:
            return None, ("unsupported_effort", rejection or "effort rejected")
        if request.resolved_model is not None and request.resolved_model != resolved:
            return None, (
                "resolved_model_mismatch",
                f"admitted resolved model {request.resolved_model!r} does not "
                f"match the catalog resolution {resolved!r}",
            )
        return resolved, None

    async def cancel(self, run_id: str, ctx: RuntimeContext) -> CancelResult:
        now = datetime.now(timezone.utc)
        confirmed = self._behavior is not MockBehavior.HANG_IGNORES_CANCEL
        if confirmed:
            self._cancelled.set()
        return CancelResult(
            run_id=run_id,
            requested=True,
            requested_at=now,
            confirmed=confirmed,
            confirmed_at=now if confirmed else None,
            deadline_seconds=1.0,
            detail=self._cancel_detail
            or (
                "mock driver acknowledged cancellation request"
                if confirmed
                else "mock driver ignores cancellation (operator-selected)"
            ),
        )

    async def aclose(self) -> None:
        self._closed = True

    def _event(self, request: NormalizedRequest, sequence: int, **kwargs) -> dict:
        return {
            "run_id": request.run_id,
            "sequence": sequence,
            "timestamp": datetime.now(timezone.utc),
            "synthetic": True,
            **kwargs,
        }

    def _delta(self, request: NormalizedRequest, sequence: int, text: str) -> RunEvent:
        return MessageDeltaEvent(
            **self._event(request, sequence),
            payload=MessageDeltaPayload(text=text),
        )

    def _started(
        self, request: NormalizedRequest, sequence: int, resolved_model: str | None = None
    ) -> RunEvent:
        return RunStartedEvent(
            **self._event(request, sequence),
            payload=RunStartedPayload(
                preset=request.preset,
                model_alias=request.model_alias,
                reasoning_effort=request.reasoning_effort,
                resolved_model=resolved_model,
            ),
        )

    def _completed(
        self, request: NormalizedRequest, sequence: int, outcome: str
    ) -> RunEvent:
        return RunCompletedEvent(
            **self._event(request, sequence),
            payload=RunCompletedPayload(
                outcome=outcome,
                usage=Usage(provenance=UsageProvenance.UNKNOWN),
                message="synthetic completion",
            ),
        )

    def _chunks(self, request: NormalizedRequest) -> list[str]:
        last = request.messages[-1].content if request.messages else ""
        return ["[synthetic] ", f"echo:{last[:40]}", " ...done"]

    async def _wait_cancelled(self, ctx: RuntimeContext) -> None:
        """Block until either the runner or a direct cancel() requests a stop."""
        waiters = [
            asyncio.ensure_future(ctx.cancellation.wait()),
            asyncio.ensure_future(self._cancelled.wait()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()

    async def execute(
        self, request: NormalizedRequest, ctx: RuntimeContext
    ) -> AsyncIterator[RunEvent]:
        behavior = self._behavior

        if behavior is MockBehavior.HANG:
            yield self._started(request, 1)
            await self._wait_cancelled(ctx)
            yield RunCancelledEvent(
                **self._event(request, 2),
                payload=RunCancelledPayload(reason="operator-selected cancellable hang"),
            )
            return

        if behavior is MockBehavior.HANG_IGNORES_CANCEL:
            # Ignores both the context cancellation and cancel(): the runner must
            # abandon this run as unknown rather than claim completion.
            await asyncio.Event().wait()
            return

        if behavior is MockBehavior.MALFORMED:
            async for event in self._execute_malformed(request):
                yield event
            return

        if behavior is MockBehavior.SLOW:
            yield self._started(request, 1)
            for index in range(SLOW_DELTAS):
                await asyncio.sleep(SLOW_DELAY_SECONDS)
                yield self._delta(request, index + 2, f"[synthetic] tick {index + 1}")
            yield self._completed(request, SLOW_DELTAS + 2, "succeeded")
            return

        if behavior is MockBehavior.MANY_EVENTS:
            yield self._started(request, 1)
            for index in range(MANY_DELTAS):
                yield self._delta(request, index + 2, f"[synthetic] e{index + 1}")
            yield self._completed(request, MANY_DELTAS + 2, "succeeded")
            return

        sequence = 1
        resolved_model, rejection = self._resolve_run_model(request)
        if rejection is not None:
            yield RunFailedEvent(
                **self._event(request, sequence),
                payload=RunFailedPayload(code=rejection[0], message=rejection[1]),
            )
            return
        yield self._started(request, sequence, resolved_model)

        if behavior is MockBehavior.CRASH:
            for text in self._chunks(request):
                sequence += 1
                yield self._delta(request, sequence, text)
            raise RuntimeError("mock abrupt crash (operator-selected)")

        for text in self._chunks(request):
            sequence += 1
            await asyncio.sleep(0)
            yield self._delta(request, sequence, text)

        if behavior is MockBehavior.FAILED:
            sequence += 1
            yield RunFailedEvent(
                **self._event(request, sequence),
                payload=RunFailedPayload(
                    code="mock_explicit_failure",
                    message="operator-selected explicit failure fixture",
                ),
            )
            return

        outcome = "partial" if behavior is MockBehavior.PARTIAL else "succeeded"
        sequence += 1
        yield self._completed(request, sequence, outcome)

    async def _execute_malformed(
        self, request: NormalizedRequest
    ) -> AsyncIterator[RunEvent]:
        mode = self._malformed_mode
        if mode is MockMalformedMode.DUPLICATE_SEQUENCE:
            yield self._delta(request, 1, "[synthetic] first")
            yield self._delta(request, 1, "[synthetic] duplicate sequence")
            return
        if mode is MockMalformedMode.EVENT_AFTER_TERMINAL:
            yield self._completed(request, 1, "succeeded")
            yield self._delta(request, 2, "[synthetic] event after terminal")
            return
        yield self._delta(request, 1, "[synthetic] no terminal follows")


__all__ = ["MockBehavior", "MockDriver", "MockMalformedMode"]
