"""Antigravity CLI (``agy``) ProviderDriver over its documented NDJSON stream.

Verified interface facts this driver relies on (see ``docs/NATIVE_PROTOCOLS.md``):

* the CLI is started as ``agy --input-format stream-json --output-format
  stream-json`` and in that mode no ``-p`` prompt argument is used;
* input is one NDJSON ``{"event": "user", "message": {"content": ...}}`` frame;
* the output envelope is discriminated by an ``event`` field and every payload
  is nested under a key matching the event name: ``{"event": "init", "init":
  {...}}``, ``{"event": "step_update", "step_update": {...}}``, ``{"event":
  "result", "result": {...}}``;
* ``init`` arrives before the prompt is sent and carries ``model``,
  ``permission_mode`` and ``cwd``; the driver verifies all three against the
  admitted request and the operator's permission decision before trusting any
  later frame;
* only ``step_update.step_type == "agent_response"`` ``text_delta`` is
  model-visible answer text. Planning, tool and progress content must never be
  forwarded as an answer delta;
* a turn ends with ``result.result.status``; ``SUCCESS`` is the only status that
  may complete a run, and observed tool errors downgrade it to ``partial``.

This driver does NOT decide the model or the executable: both come from
operator configuration (Runner launch environment). The model is bound from the
admitted request (``model_alias`` = the preset's ``model_id``; the
``antigravity/<model>`` preset alias is the fallback source) and intersected
with the operator allowlist - there is no singleton default, no fuzzy matching
and no fallback. It performs no inference of its own in tests, and it never
fabricates usage, success or verification.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from cli_provider_sdk import (
    ID_PATTERN,
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
)

CLI_ENV = "AGY_CLI"
MODELS_ENV = "AGY_MODELS"
EXPECTED_VERSION_ENV = "AGY_EXPECTED_VERSION"
ALLOW_SKIP_ENV = "AGY_ALLOW_SKIP_PERMISSIONS"
CATALOG_TTL_ENV = "AGY_CATALOG_TTL_SECONDS"

# Runner execution-config action that authorizes ``--dangerously-skip-permissions``
# for a run. It is a named operator action (not a request field): the flag is
# emitted only when the operator enabled it for this driver AND the bound
# permission policy grants the action.
SKIP_PERMISSIONS_ACTION = "antigravity.dangerously_skip_permissions"

DEFAULT_CLI = "agy"
VERSION_TIMEOUT_SECONDS = 10.0
CATALOG_TIMEOUT_SECONDS = 15.0
CATALOG_MAX_BYTES = 64 * 1024
DEFAULT_CATALOG_TTL = 300.0
HANDSHAKE_TIMEOUT_SECONDS = 15.0
POLL_SECONDS = 0.25
KNOWN_EVENTS = frozenset({"init", "step_update", "result"})
_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")
_ID_RE = re.compile(ID_PATTERN)
_CATALOG_SOURCE = "agy models (authenticated CLI catalog)"
_PRESET_PREFIX = "antigravity/"

# Operator env vars are scrubbed from the spawned CLI so a stray driver-level
# knob can never silently steer a session (the session's only inputs are the
# exact argv and the bound workspace cwd).
SANITIZED_ENV = (
    CLI_ENV,
    "AGY_MODEL",  # legacy singleton knob; no longer read, still scrubbed
    MODELS_ENV,
    EXPECTED_VERSION_ENV,
    ALLOW_SKIP_ENV,
    CATALOG_TTL_ENV,
)


def _split_models(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class AntigravityDriver(BaseDriver):
    """Stateless, one-process-per-run Antigravity driver."""

    def __init__(
        self,
        *,
        cli_command: str | None = None,
        models: list[str] | None = None,
        expected_version: str | None = None,
        allow_skip_permissions: bool | None = None,
        catalog_ttl_seconds: float | None = None,
        catalog_timeout_seconds: float = CATALOG_TIMEOUT_SECONDS,
        handshake_timeout_seconds: float = HANDSHAKE_TIMEOUT_SECONDS,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    ) -> None:
        self._cli = cli_command or os.environ.get(CLI_ENV) or DEFAULT_CLI
        raw_models = (
            list(models)
            if models is not None
            else _split_models(os.environ.get(MODELS_ENV))
        )
        # Only IDs that can ever be advertised as a ModelDescriptor are kept:
        # an allowlist entry that violates the schema ID pattern is dropped
        # rather than promoted into a descriptor that cannot validate.
        self._models = [m for m in raw_models if _ID_RE.match(m)]
        self._expected_version = (
            expected_version
            if expected_version is not None
            else os.environ.get(EXPECTED_VERSION_ENV) or None
        )
        self._allow_skip_permissions = _truthy(
            allow_skip_permissions
            if allow_skip_permissions is not None
            else os.environ.get(ALLOW_SKIP_ENV)
        )
        self._catalog_ttl = (
            float(catalog_ttl_seconds)
            if catalog_ttl_seconds is not None
            else float(os.environ.get(CATALOG_TTL_ENV, DEFAULT_CATALOG_TTL))
        )
        self._catalog_timeout = catalog_timeout_seconds
        self._handshake_timeout = handshake_timeout_seconds
        self._max_frame_bytes = max_frame_bytes
        self._grace_seconds = grace_seconds
        self._active: dict[str, NdjsonProcessTransport] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._catalog_cache: tuple[float, list[ModelDescriptor]] | None = None

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
        build is declared false rather than assumed. ``workspace_write`` is true
        only when the operator enabled the permission-bypass mode for this
        driver: under the default ``request-review`` mode the run stays
        non-writing as far as this driver is concerned.
        """
        return Capabilities(
            streaming=StreamingMode.NATIVE,
            sessions=SessionMode.NONE,
            roles=RoleMode.SERIALIZED,
            structured_output=StructuredOutputMode.NONE,
            external_tool_calls=False,
            internal_tools=True,
            vision=False,
            workspace_write=self._allow_skip_permissions,
            web_search=False,
            usage=UsageProvenance.REPORTED,
        )

    # ----------------------------------------------------------------- argv

    def _sanitized_argv(self, tail: list[str]) -> list[str]:
        argv = ["env"]
        for name in SANITIZED_ENV:
            argv += ["-u", name]
        return argv + [self._cli] + tail

    def _session_argv(self, model: str, skip_permissions: bool) -> list[str]:
        argv = self._sanitized_argv(
            [
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--model",
                model,
            ]
        )
        if skip_permissions:
            argv.append("--dangerously-skip-permissions")
        return argv

    def _event_kwargs(self, request: NormalizedRequest, sequence: int) -> dict[str, Any]:
        return {
            "run_id": request.run_id,
            "sequence": sequence,
            "timestamp": datetime.now(timezone.utc),
        }

    @staticmethod
    def _user_frame(request: NormalizedRequest) -> dict[str, Any]:
        """Serialize every message role and text; roles are not native here."""
        parts = [f"{message.role}: {message.content}" for message in request.messages]
        return {"event": "user", "message": {"content": "\n\n".join(parts)}}

    # ---------------------------------------------------------------- probe

    async def _spawned_stdout(
        self, ctx: RuntimeContext, argv: list[str], timeout: float, cap: int
    ) -> tuple[bytes | None, NdjsonProcessTransport]:
        """Spawn, read stdout bounded by ``timeout``/``cap``, then close."""
        process = await ctx.executor.spawn(argv)
        transport = NdjsonProcessTransport(
            process,
            max_frame_bytes=self._max_frame_bytes,
            grace_seconds=self._grace_seconds,
        )

        async def read_all() -> bytes:
            collected = b""
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk or len(collected) > cap:
                    return collected
                collected += chunk

        try:
            output = await asyncio.wait_for(read_all(), timeout=timeout)
        except (asyncio.TimeoutError, OSError):
            output = None
        return output, transport

    async def _cli_version(
        self, ctx: RuntimeContext
    ) -> tuple[str | None, NdjsonProcessTransport | None]:
        """Run ``agy --version`` bounded; return (parsed version, transport)."""
        try:
            output, transport = await self._spawned_stdout(
                ctx, self._sanitized_argv(["--version"]), VERSION_TIMEOUT_SECONDS, 4096
            )
        except ProcessStartError:
            return None, None
        text = (output or b"").decode("utf-8", "replace")
        match = _VERSION_RE.search(text)
        return (match.group(0) if match else None), transport

    async def probe(self, ctx: RuntimeContext) -> ProbeReport:
        notes = [
            "stream-json NDJSON transport; no -p prompt argument",
            "only step_update agent_response text_delta is treated as answer text",
            "init carries model/permission_mode/cwd and is verified before the prompt",
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

        cli_version, transport = await self._cli_version(ctx)
        if transport is None:
            return ProbeReport(
                ok=False,
                driver_id=self.manifest.driver_id,
                driver_version=self.manifest.version,
                cli_version=None,
                capabilities=self.capabilities(),
                notes=notes + ["CLI not startable"],
            )
        confirmed = await transport.aclose()
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

    async def _read_catalog(
        self, ctx: RuntimeContext
    ) -> tuple[set[str] | None, str]:
        """Run bounded ``agy models`` and return the exact observed ID set.

        Returns ``(None, reason)`` when the catalog cannot be trusted at all:
        spawn failure, timeout, nonzero exit, or zero parseable rows. Stdout and
        stderr text are never propagated - only the outcome classification is.
        """
        try:
            output, transport = await self._spawned_stdout(
                ctx,
                self._sanitized_argv(["models"]),
                self._catalog_timeout,
                CATALOG_MAX_BYTES,
            )
        except ProcessStartError:
            return None, "catalog CLI not startable"
        await transport.aclose()
        if output is None:
            return None, "catalog read timed out or failed"
        if transport.returncode not in (0, None):
            return None, "catalog command exited non-zero"
        ids: set[str] = set()
        for line in output.decode("utf-8", "replace").splitlines():
            if "\t" not in line:
                continue  # documented "Fetching available models..." preamble
            model_id = line.split("\t", 1)[0].strip()
            if model_id:
                ids.add(model_id)
        if not ids:
            return None, "catalog output parsed to zero model rows"
        return ids, "catalog read"

    def _descriptor(
        self, model_id: str, status: VerificationStatus, reason: str, label: str | None = None
    ) -> ModelDescriptor:
        return ModelDescriptor(
            model_id=model_id,
            display_name=label or model_id,
            verification=Verification(
                status=status, source=_CATALOG_SOURCE, reason=reason
            ),
        )

    async def _catalog_descriptors(self, ctx: RuntimeContext) -> list[ModelDescriptor]:
        if ctx.executor is None:
            return [
                self._descriptor(
                    m, VerificationStatus.UNKNOWN, "no process executor supplied; catalog not read"
                )
                for m in self._models
            ]
        if not self._expected_version:
            return [
                self._descriptor(
                    m, VerificationStatus.UNKNOWN, "no pinned CLI version configured (AGY_EXPECTED_VERSION)"
                )
                for m in self._models
            ]
        cli_version, transport = await self._cli_version(ctx)
        if transport is not None:
            await transport.aclose()
        if cli_version is None:
            return [
                self._descriptor(
                    m, VerificationStatus.UNKNOWN, "CLI version could not be read or parsed"
                )
                for m in self._models
            ]
        if cli_version != self._expected_version:
            return [
                self._descriptor(
                    m,
                    VerificationStatus.FAILED,
                    f"CLI version {cli_version} does not match pinned {self._expected_version}",
                )
                for m in self._models
            ]
        catalog, reason = await self._read_catalog(ctx)
        if catalog is None:
            return [
                self._descriptor(m, VerificationStatus.UNKNOWN, reason)
                for m in self._models
            ]
        descriptors = []
        for model_id in self._models:
            if model_id in catalog:
                descriptors.append(
                    self._descriptor(
                        model_id,
                        VerificationStatus.PASSED,
                        f"exact id observed in authenticated catalog; CLI {cli_version} "
                        "matches the pinned version; valid for "
                        f"{self._catalog_ttl:.0f}s (membership only - not an inference or quota claim)",
                    )
                )
            else:
                descriptors.append(
                    self._descriptor(
                        model_id,
                        VerificationStatus.FAILED,
                        "exact id not present in the authenticated catalog (no fuzzy match)",
                    )
                )
        return descriptors

    async def discover_models(self, ctx: RuntimeContext) -> list[ModelDescriptor]:
        """Verify each operator-pinned exact model id against the CLI catalog.

        A descriptor is PASSED only when the pinned CLI version matches and the
        exact id was observed in the authenticated ``agy models`` output. A
        parsed catalog without the exact id is FAILED; anything unreadable is
        UNKNOWN. Results are cached for the operator-configured TTL.
        """
        if (
            self._catalog_cache is not None
            and time.monotonic() - self._catalog_cache[0] < self._catalog_ttl
        ):
            return self._catalog_cache[1]
        descriptors = await self._catalog_descriptors(ctx)
        self._catalog_cache = (time.monotonic(), descriptors)
        return descriptors

    # -------------------------------------------------------------- execute

    def _resolve_model(
        self, request: NormalizedRequest
    ) -> tuple[str | None, tuple[str, str] | None]:
        """Resolve the exact model the run may use, or a (code, message) rejection.

        The admitted binding is ``request.model_alias`` (the preset's pinned
        ``model_id`` forwarded by the controller). When it is absent the
        ``antigravity/<model>`` preset alias supplies it. When both are present
        they must agree. Whatever resolves must be in the operator allowlist.
        """
        preset_model = None
        if request.preset.startswith(_PRESET_PREFIX):
            tail = request.preset[len(_PRESET_PREFIX) :]
            if tail and "/" not in tail:
                preset_model = tail
        if request.model_alias and preset_model and request.model_alias != preset_model:
            return None, (
                "model_alias_mismatch",
                f"admitted model {request.model_alias!r} does not match preset "
                f"{request.preset!r}",
            )
        resolved = request.model_alias or preset_model
        if not resolved:
            return None, (
                "unknown_preset",
                "preset must be antigravity/<exact model id> or carry a model_alias",
            )
        if resolved not in self._models:
            return None, (
                "unsupported_model",
                f"model {resolved!r} is not in the operator allowlist",
            )
        return resolved, None

    def _catalog_allows(self, model: str, seen: list[ModelDescriptor]) -> bool:
        for descriptor in seen:
            if descriptor.model_id == model:
                return descriptor.verification.status == VerificationStatus.PASSED
        return False

    async def execute(
        self, request: NormalizedRequest, ctx: RuntimeContext
    ) -> AsyncIterator[RunEvent]:
        sequence = 1

        def failed(code: str, message: str) -> RunFailedEvent:
            return RunFailedEvent(
                **self._event_kwargs(request, sequence),
                payload=RunFailedPayload(code=code, message=message),
            )

        if ctx.executor is None:
            yield failed(
                "no_process_executor",
                "the runner supplied no process executor; refusing to run",
            )
            return
        if not request.deadline_seconds or request.deadline_seconds <= 0:
            yield failed("no_deadline", "a finite deadline is required to run the CLI")
            return
        model, rejection = self._resolve_model(request)
        if rejection is not None:
            yield failed(rejection[0], rejection[1])
            return
        if ctx.workspace is None or not ctx.workspace.root:
            yield failed(
                "no_workspace", "the runner supplied no bound workspace; refusing to run"
            )
            return

        # Reuse the (TTL-cached) catalog verification so a run never launches a
        # model that is not a PASSED catalog member.
        if not self._catalog_allows(model, await self.discover_models(ctx)):
            yield failed(
                "catalog_not_verified",
                f"model {model!r} is not a verified catalog member; refusing to run",
            )
            return

        skip_permissions = bool(
            self._allow_skip_permissions
            and ctx.permissions is not None
            and ctx.permissions.allows(SKIP_PERMISSIONS_ACTION)
        )
        expected_mode = "always-proceed" if skip_permissions else "request-review"
        workspace_root = os.path.realpath(ctx.workspace.root)

        yield RunStartedEvent(
            **self._event_kwargs(request, sequence),
            payload=RunStartedPayload(
                preset=request.preset, model_alias=model
            ),
        )
        sequence += 1

        try:
            process = await ctx.executor.spawn(
                self._session_argv(model, skip_permissions), cwd=workspace_root
            )
        except ProcessStartError as exc:
            yield failed("cli_not_startable", str(exc))
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
        saw_denial = False
        terminal_sent = False
        tools_seen: set[str] = set()

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
            # ---- handshake: the first frame must be a verified init -------
            handshake_deadline = min(
                deadline_at, time.monotonic() + self._handshake_timeout
            )
            while True:
                if ctx.cancellation.is_requested() or cancel_event.is_set():
                    for event in await forced_stop("cancelled", "cancellation requested"):
                        yield event
                        terminal_sent = True
                    return
                remaining = handshake_deadline - time.monotonic()
                if remaining <= 0:
                    for event in await forced_stop(
                        "missing_init",
                        "CLI did not emit its init frame within the handshake bound",
                    ):
                        yield event
                        terminal_sent = True
                    return
                try:
                    frame = await transport.recv_within(min(remaining, POLL_SECONDS))
                except asyncio.TimeoutError:
                    continue
                except (MalformedFrame, FrameTooLarge) as exc:
                    for event in await forced_stop(
                        "protocol_error", f"invalid CLI frame: {exc}"
                    ):
                        yield event
                        terminal_sent = True
                    return
                if frame is None:
                    for event in await forced_stop(
                        "missing_init", "CLI stream ended before its init frame"
                    ):
                        yield event
                        terminal_sent = True
                    return
                if frame.get("event") != "init" or not isinstance(
                    frame.get("init"), dict
                ):
                    for event in await forced_stop(
                        "protocol_error",
                        "first CLI frame was not a valid init envelope",
                    ):
                        yield event
                        terminal_sent = True
                    return
                init = frame["init"]
                observed_model = init.get("model")
                if observed_model != model:
                    for event in await forced_stop(
                        "model_mismatch",
                        f"CLI init reported model {observed_model!r}; expected {model!r}",
                    ):
                        yield event
                        terminal_sent = True
                    return
                observed_mode = init.get("permission_mode")
                if observed_mode != expected_mode:
                    for event in await forced_stop(
                        "permission_mode_mismatch",
                        f"CLI init reported permission_mode {observed_mode!r}; "
                        f"expected {expected_mode!r}",
                    ):
                        yield event
                        terminal_sent = True
                    return
                observed_cwd = init.get("cwd")
                if not isinstance(observed_cwd, str) or os.path.realpath(
                    observed_cwd
                ) != workspace_root:
                    for event in await forced_stop(
                        "cwd_mismatch",
                        f"CLI init reported cwd {observed_cwd!r}; expected the bound workspace",
                    ):
                        yield event
                        terminal_sent = True
                    return
                break

            # ---- prompt ----------------------------------------------------
            try:
                await asyncio.wait_for(
                    transport.send(self._user_frame(request)),
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

            # ---- turn stream -----------------------------------------------
            while True:
                if ctx.cancellation.is_requested() or cancel_event.is_set():
                    for event in await forced_stop("cancelled", "cancellation requested"):
                        yield event
                        terminal_sent = True
                    return

                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    for event in await forced_stop("deadline", "run deadline expired"):
                        yield event
                        terminal_sent = True
                    return

                try:
                    frame = await transport.recv_within(min(remaining, POLL_SECONDS))
                except asyncio.TimeoutError:
                    continue
                except (MalformedFrame, FrameTooLarge) as exc:
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
                    continue  # session already verified; a repeat is inert

                if event == "step_update":
                    step_events, denied = self._step_events(
                        request, sequence, frame, tools_seen
                    )
                    saw_denial = saw_denial or denied
                    if step_events is None:
                        for event_ in await forced_stop(
                            "protocol_error",
                            "step_update frame was missing its nested payload",
                        ):
                            yield event_
                            terminal_sent = True
                        return
                    for step_event in step_events:
                        yield step_event
                        sequence = step_event.sequence + 1
                    continue

                # event == "result"
                payload = frame.get("result")
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("status"), str
                ):
                    for event_ in await forced_stop(
                        "protocol_error", "result frame was missing its status"
                    ):
                        yield event_
                        terminal_sent = True
                    return
                status = payload["status"]
                if status != "SUCCESS":
                    for event_ in await forced_stop(
                        "cli_reported_error",
                        f"the CLI reported terminal status {status!r}",
                    ):
                        yield event_
                        terminal_sent = True
                    return
                await transport.terminate()
                outcome = "partial" if saw_denial else "succeeded"
                yield RunCompletedEvent(
                    **self._event_kwargs(request, sequence),
                    payload=RunCompletedPayload(
                        outcome=outcome,
                        usage=self._result_usage(payload),
                        message=(
                            "turn finished after a tool error/denial; downgraded to partial"
                            if saw_denial
                            else "turn finished; SUCCESS status verified"
                        ),
                    ),
                )
                terminal_sent = True
                return
        finally:
            self._active.pop(request.run_id, None)
            self._cancel_events.pop(request.run_id, None)
            await transport.aclose()
            if not terminal_sent:
                ctx.logger.warning(
                    "antigravity run ended without a terminal event",
                    run_id=request.run_id,
                )

    # ------------------------------------------------------------- mapping

    def _step_events(
        self,
        request: NormalizedRequest,
        sequence: int,
        frame: dict[str, Any],
        tools_seen: set[str],
    ) -> tuple[list[RunEvent] | None, bool]:
        """Map a nested ``step_update`` payload.

        Returns ``(events, denied)``; ``events`` is ``None`` when the frame is
        structurally invalid (payload missing/not an object). Only
        ``agent_response`` ``text_delta`` becomes answer text; every other step
        type - user_input, checkpoint, thought, unknown kinds - is consumed and
        ignored so it can never leak into the answer.
        """
        step = frame.get("step_update")
        if not isinstance(step, dict):
            return None, False
        events: list[RunEvent] = []
        denied = False
        step_type = step.get("step_type")
        seq = sequence

        def stamp() -> dict[str, Any]:
            nonlocal seq
            stamp = {
                "run_id": request.run_id,
                "sequence": seq,
                "timestamp": datetime.now(timezone.utc),
            }
            seq += 1
            return stamp

        if step_type == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                events.append(
                    MessageDeltaEvent(
                        **stamp(), payload=MessageDeltaPayload(text=delta)
                    )
                )
        elif step_type == "tool":
            index = step.get("step_index")
            identifier = (
                f"tool-{index}" if isinstance(index, int) else f"tool-{sequence}"
            )
            info = step.get("tool_info")
            info = info if isinstance(info, dict) else {}
            name = step.get("tool_name") or info.get("name") or "tool"
            state = step.get("state")
            errored = bool(info.get("error")) or bool(step.get("error"))
            if state == "ACTIVE" and identifier not in tools_seen:
                tools_seen.add(identifier)
                events.append(
                    ToolStartedEvent(
                        **stamp(),
                        payload=ToolStartedPayload(
                            tool_call_id=identifier, name=str(name)
                        ),
                    )
                )
            elif state == "DONE":
                if identifier not in tools_seen:
                    tools_seen.add(identifier)
                    events.append(
                        ToolStartedEvent(
                            **stamp(),
                            payload=ToolStartedPayload(
                                tool_call_id=identifier, name=str(name)
                            ),
                        )
                    )
                status = "failed" if errored else "completed"
                events.append(
                    ToolCompletedEvent(
                        **stamp(),
                        payload=ToolCompletedPayload(
                            tool_call_id=identifier, status=status
                        ),
                    )
                )
                denied = denied or errored
        return events, denied

    @staticmethod
    def _result_usage(payload: dict[str, Any]) -> Usage:
        """First-turn usage from the result frame, or honestly UNKNOWN.

        ``result.usage`` is cumulative in persistent sessions; this driver is
        stateless (one process per run), so a valid non-zero observation is the
        first-turn usage. Absent, malformed or all-zero usage is UNKNOWN - never
        a fabricated zero.
        """
        usage = payload.get("usage")
        if isinstance(usage, dict):
            def _count(key: str) -> int | None:
                value = usage.get(key)
                if isinstance(value, bool) or not isinstance(value, int):
                    return None
                return value if value >= 0 else None

            input_tokens = _count("input_tokens")
            output_tokens = _count("output_tokens")
            if (input_tokens or 0) + (output_tokens or 0) > 0:
                return Usage(
                    provenance=UsageProvenance.REPORTED,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
        return Usage(provenance=UsageProvenance.UNKNOWN)

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
