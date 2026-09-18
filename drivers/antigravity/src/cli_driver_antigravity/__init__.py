"""Antigravity CLI (``agy``) ProviderDriver over its documented NDJSON stream.

Verified interface facts this driver relies on (see ``docs/NATIVE_PROTOCOLS.md``):

* the CLI is started as ``agy --input-format stream-json --output-format
  stream-json`` and in that mode no ``-p`` prompt argument is used;
* input is one NDJSON user frame per turn;
* the output envelope is discriminated by an ``event`` field; the first frame is
  ``{"event": "init", "conversation_id": ..., "init": {...}}`` and each turn ends
  with a ``result`` frame;
* only ``agent_response.text_delta`` is model-visible answer text. Planning,
  tool and progress content must never be forwarded as an answer delta.

This driver does NOT decide the model or the executable: both come from operator
configuration (Runner launch environment). It performs no inference of its own in
tests, and it never fabricates usage, success or verification.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from cli_provider_sdk import (
    BaseDriver,
    CancelResult,
    Capabilities,
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
    SDK_VERSION,
    SessionMode,
    StreamingMode,
    StructuredOutputMode,
    ToolCompletedEvent,
    ToolCompletedPayload,
    ToolStartedEvent,
    ToolStartedPayload,
    TransportKind,
    Usage,
    UsageProvenance,
    Verification,
    VerificationStatus,
)
from cli_provider_transports import (
    DEFAULT_MAX_FRAME_BYTES,
    DEFAULT_TERMINATION_GRACE_SECONDS,
    FrameTooLarge,
    MalformedFrame,
    NdjsonProcessTransport,
    ProcessStartError,
    TransportError,
)

CLI_ENV = "AGY_CLI"
MODEL_ENV = "AGY_MODEL"
MODELS_ENV = "AGY_MODELS"
EXPECTED_VERSION_ENV = "AGY_EXPECTED_VERSION"

DEFAULT_CLI = "agy"
VERSION_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.25
KNOWN_EVENTS = frozenset({"init", "step_update", "result"})
_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")
_CATALOG_SOURCE = "operator-pinned model id"


def _split_env_models(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


class AntigravityDriver(BaseDriver):
    """Stateless, one-process-per-run Antigravity driver."""

    def __init__(
        self,
        *,
        cli_command: str | None = None,
        model: str | None = None,
        models: list[str] | None = None,
        expected_version: str | None = None,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    ) -> None:
        self._cli = cli_command or os.environ.get(CLI_ENV) or DEFAULT_CLI
        self._model = model if model is not None else os.environ.get(MODEL_ENV)
        self._models = (
            list(models)
            if models is not None
            else _split_env_models(os.environ.get(MODELS_ENV))
        )
        self._expected_version = (
            expected_version
            if expected_version is not None
            else os.environ.get(EXPECTED_VERSION_ENV)
        )
        self._max_frame_bytes = max_frame_bytes
        self._grace_seconds = grace_seconds
        self._active: dict[str, NdjsonProcessTransport] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------- manifest

    @property
    def manifest(self) -> DriverManifest:
        return DriverManifest(
            driver_id="antigravity",
            name="Antigravity CLI (agy) NDJSON driver",
            version="0.1.0",
            sdk_version=SDK_VERSION,
            protocol_family="antigravity-ndjson",
            supported_transports=[TransportKind.STDIO],
            synthetic=False,
        )

    def capabilities(self) -> Capabilities:
        """Declared capability matrix.

        Deliberately conservative: everything not exercised by fixtures in this
        build is declared false rather than assumed, so no caller can treat an
        unverified ability as available. ``workspace_write`` stays false until an
        operator canary has exercised the CLI's own permission handling.
        """
        return Capabilities(
            streaming=StreamingMode.NATIVE,
            sessions=SessionMode.NONE,
            roles=RoleMode.SERIALIZED,
            # Structured output is NOT validated by this driver yet, so it must
            # not be advertised: only the documented answer-text mapping exists.
            structured_output=StructuredOutputMode.NONE,
            external_tool_calls=False,
            internal_tools=True,
            vision=False,
            workspace_write=False,
            web_search=False,
            usage=UsageProvenance.UNKNOWN,
        )

    # ---------------------------------------------------------------- probe

    async def probe(self, ctx: RuntimeContext) -> ProbeReport:
        notes = [
            "stream-json NDJSON transport; no -p prompt argument",
            "only agent_response.text_delta is treated as answer text",
        ]
        if ctx.executor is None:
            return ProbeReport(
                ok=False,
                driver_id=self.manifest.driver_id,
                driver_version=self.manifest.version,
                cli_version=None,
                capabilities=self.capabilities(),
                notes=notes + ["no process executor supplied; CLI not probed"],
            )

        try:
            process = await ctx.executor.spawn([self._cli, "--version"])
        except ProcessStartError as exc:
            return ProbeReport(
                ok=False,
                driver_id=self.manifest.driver_id,
                driver_version=self.manifest.version,
                cli_version=None,
                capabilities=self.capabilities(),
                notes=notes + [f"CLI not startable: {exc}"],
            )

        transport = NdjsonProcessTransport(
            process,
            max_frame_bytes=self._max_frame_bytes,
            grace_seconds=self._grace_seconds,
        )
        output = b""

        async def read_version_output() -> bytes:
            # One aggregate budget: a CLI dribbling bytes just under a per-read
            # timeout must not keep probe() alive indefinitely.
            collected = b""
            while True:
                chunk = await process.stdout.read(256)
                if not chunk or len(collected) > 4096:
                    return collected
                collected += chunk

        try:
            output = await asyncio.wait_for(
                read_version_output(), timeout=VERSION_TIMEOUT_SECONDS
            )
        except (asyncio.TimeoutError, OSError):
            notes.append("version read timed out")
        finally:
            confirmed = await transport.aclose()

        text = output.decode("utf-8", "replace")
        match = _VERSION_RE.search(text)
        cli_version = match.group(0) if match else None
        notes.append(f"exit confirmed: {confirmed}")
        notes.append(transport.stderr_classification())

        if cli_version is None:
            return ProbeReport(
                ok=False,
                driver_id=self.manifest.driver_id,
                driver_version=self.manifest.version,
                cli_version=None,
                capabilities=self.capabilities(),
                notes=notes + ["CLI version could not be parsed; refusing to guess"],
            )
        if self._expected_version and cli_version != self._expected_version:
            return ProbeReport(
                ok=False,
                driver_id=self.manifest.driver_id,
                driver_version=self.manifest.version,
                cli_version=cli_version,
                capabilities=self.capabilities(),
                notes=notes
                + [
                    f"CLI version {cli_version} does not match pinned "
                    f"{self._expected_version}"
                ],
            )
        return ProbeReport(
            ok=True,
            driver_id=self.manifest.driver_id,
            driver_version=self.manifest.version,
            cli_version=cli_version,
            capabilities=self.capabilities(),
            notes=notes,
        )

    # ------------------------------------------------------------- discovery

    async def discover_models(self, ctx: RuntimeContext) -> list[ModelDescriptor]:
        """Operator-pinned exact model ids only.

        The authenticated catalog is not read here: a preset therefore stays
        unverified until an operator canary confirms the exact id, and an empty
        pin list means no model is available rather than a guessed default.
        """
        return [
            ModelDescriptor(
                model_id=model_id,
                display_name=f"{model_id} (operator-pinned, unverified)",
                verification=Verification(
                    status=VerificationStatus.UNKNOWN,
                    source=_CATALOG_SOURCE,
                    reason="no authenticated catalog verification performed",
                ),
            )
            for model_id in self._models
        ]

    # -------------------------------------------------------------- execute

    def _event_kwargs(self, request: NormalizedRequest, sequence: int) -> dict[str, Any]:
        return {
            "run_id": request.run_id,
            "sequence": sequence,
            "timestamp": datetime.now(timezone.utc),
        }

    def _argv(self) -> list[str]:
        argv = [self._cli, "--input-format", "stream-json", "--output-format", "stream-json"]
        if self._model:
            argv += ["--model", self._model]
        return argv

    @staticmethod
    def _user_frame(request: NormalizedRequest) -> dict[str, Any]:
        """Serialize every message role and text; roles are not native here."""
        parts = [f"{message.role}: {message.content}" for message in request.messages]
        return {"event": "user", "message": {"content": "\n\n".join(parts)}}

    async def execute(
        self, request: NormalizedRequest, ctx: RuntimeContext
    ) -> AsyncIterator[RunEvent]:
        if ctx.executor is None:
            yield RunFailedEvent(
                **self._event_kwargs(request, 1),
                payload=RunFailedPayload(
                    code="no_process_executor",
                    message="the runner supplied no process executor; refusing to run",
                ),
            )
            return
        if not request.deadline_seconds or request.deadline_seconds <= 0:
            yield RunFailedEvent(
                **self._event_kwargs(request, 1),
                payload=RunFailedPayload(
                    code="no_deadline",
                    message="a finite deadline is required to run the CLI",
                ),
            )
            return

        cwd = ctx.workspace.root if ctx.workspace is not None else None
        try:
            process = await ctx.executor.spawn(self._argv(), cwd=cwd)
        except ProcessStartError as exc:
            yield RunFailedEvent(
                **self._event_kwargs(request, 1),
                payload=RunFailedPayload(code="cli_not_startable", message=str(exc)),
            )
            return

        transport = NdjsonProcessTransport(
            process,
            max_frame_bytes=self._max_frame_bytes,
            grace_seconds=self._grace_seconds,
        )
        self._active[request.run_id] = transport
        cancel_event = asyncio.Event()
        self._cancel_events[request.run_id] = cancel_event
        deadline_at = time.monotonic() + request.deadline_seconds
        sequence = 1
        saw_denial = False
        saw_init = False
        terminal_sent = False

        async def forced_stop(code: str, message: str) -> list[RunEvent]:
            """Terminal events for a run we are stopping ourselves.

            Every abnormal end (EOF, a stream truncated mid-frame, a prompt write
            that fails because the CLI was killed, an undocumented frame) funnels
            through here so the *reason* we stopped is never relabelled as a
            provider failure. A stop that was requested by a cancel or an expired
            deadline becomes ``run.cancelled`` only when termination is confirmed;
            an unconfirmed stop ends without a terminal event, so the Runner
            records the attempt as ``unknown`` instead of inventing an outcome.
            """
            stopping = ctx.cancellation.is_requested() or cancel_event.is_set()
            expired = time.monotonic() >= deadline_at
            confirmed = await transport.aclose()
            if stopping or expired:
                if not confirmed:
                    return []
                reason = (
                    "run deadline expired; CLI stopped"
                    if expired and not stopping
                    else "cancellation confirmed; CLI stopped"
                )
                survivors = transport.surviving_group_members()
                if survivors:
                    # Report survivors on the deadline path too, not only in
                    # cancel(): a descendant that ignores SIGTERM is left alone
                    # on purpose and the operator must be able to see it.
                    reason += (
                        f"; {len(survivors)} group member(s) still alive and "
                        "deliberately not chased"
                    )
                return [
                    RunCancelledEvent(
                        **self._event_kwargs(request, sequence),
                        payload=RunCancelledPayload(reason=reason),
                    )
                ]
            return [
                RunFailedEvent(
                    **self._event_kwargs(request, sequence),
                    payload=RunFailedPayload(code=code, message=message),
                )
            ]

        try:
            yield RunStartedEvent(
                **self._event_kwargs(request, sequence),
                payload=RunStartedPayload(
                    preset=request.preset, model_alias=request.model_alias
                ),
            )
            sequence += 1
            try:
                await asyncio.wait_for(
                    transport.send(self._user_frame(request)),
                    # The prompt write is bounded by the same finite deadline: a CLI
                    # that never reads its stdin must not block past it.
                    timeout=max(deadline_at - time.monotonic(), 0.001),
                )
            except asyncio.TimeoutError:
                for event in await forced_stop(
                    "send_timeout", "the CLI did not read the prompt before the deadline"
                ):
                    yield event
                    terminal_sent = True
                return
            except OSError as exc:
                for event in await forced_stop(
                    "cli_broken_pipe",
                    f"the CLI closed its input before the prompt: {type(exc).__name__}",
                ):
                    yield event
                    terminal_sent = True
                return

            while True:
                if ctx.cancellation.is_requested() or cancel_event.is_set():
                    for event in await forced_stop(
                        "cancelled", "cancellation requested"
                    ):
                        yield event
                        terminal_sent = True
                    return

                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    for event in await forced_stop(
                        "deadline", "run deadline expired"
                    ):
                        yield event
                        terminal_sent = True
                    return

                try:
                    frame = await transport.recv_within(min(remaining, POLL_SECONDS))
                except asyncio.TimeoutError:
                    continue
                except (MalformedFrame, FrameTooLarge) as exc:
                    # A kill can truncate a frame mid-write, so this may be our own
                    # cancellation rather than a broken CLI.
                    for event in await forced_stop(
                        "protocol_error", f"invalid CLI frame: {exc}"
                    ):
                        yield event
                        terminal_sent = True
                    return

                if frame is None:
                    for event in await forced_stop(
                        "missing_result", "CLI stream ended without a result frame"
                    ):
                        yield event
                        terminal_sent = True
                    return

                event = frame.get("event")
                if not isinstance(event, str) or event not in KNOWN_EVENTS:
                    for event_ in await forced_stop(
                        "unknown_event",
                        "CLI emitted a frame outside the documented protocol",
                    ):
                        yield event_
                        terminal_sent = True
                    return

                if event == "init":
                    saw_init = True
                    continue

                if not saw_init:
                    # The documented order is init-first; anything else is a
                    # stream we refuse to interpret rather than guess at.
                    for event_ in await forced_stop(
                        "protocol_error", "CLI emitted a frame before its init frame"
                    ):
                        yield event_
                        terminal_sent = True
                    return

                if event == "step_update":
                    delta = self._text_delta(frame)
                    if delta:
                        yield MessageDeltaEvent(
                            **self._event_kwargs(request, sequence),
                            payload=MessageDeltaPayload(text=delta),
                        )
                        sequence += 1
                        continue
                    tool_events, denied = self._tool_events(request, sequence, frame)
                    saw_denial = saw_denial or denied
                    for tool_event in tool_events:
                        yield tool_event
                        sequence += 1
                    continue

                # event == "result": the turn finished. The terminal is emitted
                # regardless of how the process exits (the CLI is done), so the
                # termination result is not consulted here; the finally block
                # still stops and closes the transport.
                await transport.terminate()
                if self._is_error_result(frame):
                    yield RunFailedEvent(
                        **self._event_kwargs(request, sequence),
                        payload=RunFailedPayload(
                            code="cli_reported_error",
                            message="the CLI reported an unsuccessful result",
                        ),
                    )
                else:
                    outcome = "partial" if saw_denial else "succeeded"
                    yield RunCompletedEvent(
                        **self._event_kwargs(request, sequence),
                        payload=RunCompletedPayload(
                            outcome=outcome,
                            usage=Usage(provenance=UsageProvenance.UNKNOWN),
                            message=(
                                "turn finished after a tool was denied; the CLI may "
                                "still exit successfully"
                                if saw_denial
                                else "turn finished; no verification evidence collected"
                            ),
                        ),
                    )
                terminal_sent = True
                return
        finally:
            self._active.pop(request.run_id, None)
            self._cancel_events.pop(request.run_id, None)
            # Always terminate and release the pipes: an unclosed subprocess
            # transport outlives the event loop and leaks its descriptors.
            await transport.aclose()
            if not terminal_sent:
                # Leaving without a terminal event is intentional only for an
                # unconfirmed termination; the runner then records unknown.
                ctx.logger.warning(
                    "antigravity run ended without a terminal event",
                    run_id=request.run_id,
                )

    # ------------------------------------------------------------- mapping

    @staticmethod
    def _text_delta(frame: dict[str, Any]) -> str | None:
        """Only ``agent_response.text_delta`` is answer text.

        There is deliberately no fallback to a step update's own ``text_delta``:
        planning/thought/checkpoint steps may carry similar fields, and treating
        those as answer text would leak internal reasoning to the caller.
        """
        response = frame.get("agent_response")
        if not isinstance(response, dict):
            return None
        delta = response.get("text_delta")
        if isinstance(delta, str) and delta:
            return delta
        return None

    @staticmethod
    def _tool_events(
        request: NormalizedRequest, sequence: int, frame: dict[str, Any]
    ) -> tuple[list[RunEvent], bool]:
        """Map tool/planning step kinds. Planning text is never answer text."""
        events: list[RunEvent] = []
        update = frame.get("update")
        if not isinstance(update, dict):
            return events, False
        kind = update.get("type")
        identifier = str(update.get("id") or update.get("tool_call_id") or "tool")
        if kind == "tool_call":
            events.append(
                ToolStartedEvent(
                    run_id=request.run_id,
                    sequence=sequence,
                    timestamp=datetime.now(timezone.utc),
                    payload=ToolStartedPayload(
                        tool_call_id=identifier, name=str(update.get("name") or "tool")
                    ),
                )
            )
        elif kind == "tool_result":
            status = "completed" if update.get("status") in (None, "completed") else "failed"
            events.append(
                ToolCompletedEvent(
                    run_id=request.run_id,
                    sequence=sequence,
                    timestamp=datetime.now(timezone.utc),
                    payload=ToolCompletedPayload(tool_call_id=identifier, status=status),
                )
            )
        denied = kind in ("permission_denied", "tool_denied")
        return events, denied

    @staticmethod
    def _is_error_result(frame: dict[str, Any]) -> bool:
        if frame.get("is_error") is True:
            return True
        if isinstance(frame.get("error"), (str, dict)):
            return True
        status = frame.get("status")
        return isinstance(status, str) and status.lower() in ("error", "failed")

    # ---------------------------------------------------------------- cancel

    async def cancel(self, run_id: str, ctx: RuntimeContext) -> CancelResult:
        now = datetime.now(timezone.utc)
        event = self._cancel_events.get(run_id)
        if event is not None:
            event.set()
        transport = self._active.get(run_id)
        if transport is None:
            return CancelResult(
                run_id=run_id,
                requested=True,
                requested_at=now,
                confirmed=False,
                confirmed_at=None,
                deadline_seconds=1.0,
                detail="no active CLI process for this run",
            )
        confirmed = await transport.terminate()
        survivors = transport.surviving_group_members() if confirmed else []
        if not confirmed:
            detail = "termination could not be confirmed"
        elif survivors:
            # Report the truth: a descendant that ignores SIGTERM and outlives a
            # promptly-exiting leader is not chased with a group kill (the pgid
            # may already be reusable), so it must not be claimed as cleaned up.
            detail = (
                "CLI leader exit confirmed; "
                f"{len(survivors)} group member(s) still alive and deliberately "
                "not chased"
            )
        else:
            detail = "CLI process group terminated and exit confirmed"
        return CancelResult(
            run_id=run_id,
            requested=True,
            requested_at=now,
            confirmed=confirmed,
            confirmed_at=datetime.now(timezone.utc) if confirmed else None,
            deadline_seconds=1.0,
            detail=detail,
        )

    async def aclose(self) -> None:
        for transport in list(self._active.values()):
            await transport.terminate()
        self._active.clear()
        self._cancel_events.clear()
