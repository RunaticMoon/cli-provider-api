"""Devin CLI (``devin acp``) ProviderDriver over the official ACP JSON-RPC stdio.

Verified interface facts this driver relies on (``docs/NATIVE_PROTOCOLS.md`` and
the operator probe evidence captured alongside it):

* the CLI is spawned as ``devin --permission-mode <mode> acp --model <exact-id>``
  and speaks newline-delimited JSON-RPC 2.0 on stdio at ACP ``protocolVersion``
  1;
* ``initialize`` returns agent capabilities; ``agentInfo.version`` is a build
  string (``0.0.0-dev``) and is NEVER used as the distribution version - the
  real version comes from ``devin --version``;
* ``session/new`` returns ``sessionId``, ``modes`` (``currentModeId`` +
  ``availableModes``) and ``configOptions`` whose ``model`` select carries the
  effective ``currentValue`` - note that even ``--permission-mode dangerous``
  argv leaves the ACP session at ``accept-edits``, so argv is not the safety
  boundary;
* ``session/set_mode`` returns ``{}`` but a mode is only trusted once a
  ``session/update`` ``current_mode_update`` notification acknowledges it;
* ``session/prompt`` ends with a ``stopReason``; answer text is only
  ``agent_message_chunk`` text content - never thought chunks, tool logs or
  stderr;
* ``session/request_permission`` is an agent->client request. There is no
  same-run approval channel in this slice, so every request is denied
  explicitly (a ``reject_*`` option when offered, else the ``cancelled``
  outcome);
* ``session/cancel`` is a notification; a later ``cancelled`` stop reason is
  not proof the process stopped - only confirmed termination counts.

Safety posture: the executed model is the exact request-admitted id (default
pin ``swe-2-max``), gated by the operator allowlist (``DEVIN_MODELS``, ``*``
opts in to the whole verified catalog) and admitted cost tiers
(``DEVIN_ALLOWED_COST_TIERS``, default ``Free``); the observed catalog exposes
no per-model effort option so an explicit ``reasoning_effort`` is rejected,
``DEVIN_REFUSAL_FALLBACK`` and the other ``DEVIN_*`` model/permission overrides
are stripped from the child environment, ``bypass`` mode requires an explicit
injected permission policy, and usage/cost is never invented.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from cli_provider_sdk import (
    ArtifactCreatedEvent,
    ArtifactCreatedPayload,
    BaseDriver,
    CancelResult,
    Capabilities,
    DriverManifest,
    MessageDeltaEvent,
    MessageDeltaPayload,
    ModelDescriptor,
    NormalizedRequest,
    PermissionRequiredEvent,
    PermissionRequiredPayload,
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

CLI_ENV = "DEVIN_CLI"
MODEL_ENV = "DEVIN_MODEL"
MODELS_ENV = "DEVIN_MODELS"
EXPECTED_VERSION_ENV = "DEVIN_EXPECTED_VERSION"
WORKSPACE_ENV = "DEVIN_WORKSPACE_ROOT"
SESSION_MODE_ENV = "DEVIN_ACP_MODE"
CATALOG_TTL_ENV = "DEVIN_CATALOG_TTL_SECONDS"
EXPECTED_COST_ENV = "DEVIN_EXPECTED_COST_TIER"
ALLOWED_TIERS_ENV = "DEVIN_ALLOWED_COST_TIERS"

DEFAULT_CLI = "devin"
DEFAULT_MODEL = "swe-2-max"
DEFAULT_SESSION_MODE = "accept-edits"
DEFAULT_COST_TIER = "Free"
# Any exact catalog id may be authorized when the operator sets DEVIN_MODELS=*
# (still gated by the cost-tier policy); a comma list restricts to those ids.
ALLOW_ALL = "*"

# Never let inherited environment silently change the child's model, its
# refusal-fallback chain, or its permission default: the driver pins all three
# explicitly.
SANITIZED_ENV = ("DEVIN_REFUSAL_FALLBACK", "DEVIN_MODEL", "DEVIN_PERMISSION_MODE")
ARGV_PERMISSION_MODE = "accept-edits"
ACP_PROTOCOL_VERSION = 1
CLIENT_INFO = {"name": "cli-provider-devin", "version": "0.1.0"}

BYPASS_MODE = "bypass"
BYPASS_ACTION = "devin.acp.session_mode.bypass"

VERSION_TIMEOUT_SECONDS = 10.0
DEFAULT_HANDSHAKE_TIMEOUT = 15.0
DEFAULT_CATALOG_TTL_SECONDS = 300.0
POLL_SECONDS = 0.25
CANCEL_NOTIFY_SECONDS = 1.0
CANCEL_TURN_GRACE_SECONDS = 2.0

STOP_PARTIAL = frozenset({"max_tokens", "max_turn_requests", "refusal"})
STOP_KNOWN = frozenset({"end_turn", "cancelled"}) | STOP_PARTIAL
_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?")


class _Eof(Exception):
    """The CLI stream ended at a frame boundary before the awaited response."""


class _Cancelled(Exception):
    """Cancellation was requested while the pump was active."""


class _DeadlineExceeded(Exception):
    """The run deadline expired while the pump was active."""


class _PhaseTimeout(Exception):
    """A bounded handshake phase expired before its acknowledgement."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _RpcError(Exception):
    """The agent returned a JSON-RPC error for one of our requests."""


class _ProtocolError(Exception):
    """The agent emitted a frame outside the documented protocol shape."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ModeAckFailed(Exception):
    """A mode switch was acknowledged with a different mode id."""


class _RunState:
    """Per-run mutable ACP session state (facts only once acknowledged)."""

    def __init__(self) -> None:
        self.sequence = 0
        self.rpc_id = 0
        self.awaiting: Any = None
        self.response: Any = None
        self.session_id: str | None = None
        self.current_mode: str | None = None
        self.model_value: str | None = None
        self.expected_model: str | None = None
        self.awaiting_mode: str | None = None
        self.denied = False
        self.facts_recorded = False
        self.cancel_event = asyncio.Event()
        self.transport: NdjsonProcessTransport | None = None

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    def next_rpc_id(self) -> int:
        # JSON-RPC ids live in their own counter: sharing the event sequence
        # would leave gaps the Runner's monotonic-sequence check would reject.
        self.rpc_id += 1
        return self.rpc_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _split_env(raw: str | None) -> str | None:
    value = raw.strip() if raw else ""
    return value or None


def _float_env(raw: str | None, default: float) -> float:
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class DevinDriver(BaseDriver):
    """One ACP subprocess per run; no session resume in this slice."""

    def __init__(
        self,
        *,
        cli_command: str | None = None,
        model: str | None = None,
        models: list[str] | None = None,
        expected_version: str | None = None,
        workspace_root: str | None = None,
        session_mode: str | None = None,
        expected_cost_tier: str | None = None,
        allowed_cost_tiers: list[str] | None = None,
        catalog_ttl_seconds: float | None = None,
        handshake_timeout_seconds: float = DEFAULT_HANDSHAKE_TIMEOUT,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    ) -> None:
        self._cli = cli_command or os.environ.get(CLI_ENV) or DEFAULT_CLI
        self._model = (
            model if model is not None else _split_env(os.environ.get(MODEL_ENV))
        ) or DEFAULT_MODEL
        # Operator execution policy — discovery lists the real catalog, this
        # decides what may run. Default preserves the legacy pin: only the
        # configured model at the expected cost tier is admitted.
        raw_models = (
            [m.strip() for m in models if m.strip()]
            if models is not None
            else [
                m.strip()
                for m in (os.environ.get(MODELS_ENV) or "").split(",")
                if m.strip()
            ]
        )
        self._allow_all_models = ALLOW_ALL in raw_models
        self._allowed_models = frozenset(
            m for m in raw_models if m != ALLOW_ALL
        ) or frozenset({self._model})
        self._expected_cost_tier = (
            expected_cost_tier
            if expected_cost_tier is not None
            else _split_env(os.environ.get(EXPECTED_COST_ENV))
        ) or DEFAULT_COST_TIER
        raw_tiers = (
            [t.strip() for t in allowed_cost_tiers if t.strip()]
            if allowed_cost_tiers is not None
            else [
                t.strip()
                for t in (os.environ.get(ALLOWED_TIERS_ENV) or "").split(",")
                if t.strip()
            ]
        )
        self._allowed_cost_tiers = frozenset(raw_tiers) or frozenset(
            {self._expected_cost_tier}
        )
        self._expected_version = (
            expected_version
            if expected_version is not None
            else _split_env(os.environ.get(EXPECTED_VERSION_ENV))
        )
        self._workspace_root = (
            workspace_root
            if workspace_root is not None
            else _split_env(os.environ.get(WORKSPACE_ENV))
        )
        self._session_mode = (
            session_mode
            if session_mode is not None
            else _split_env(os.environ.get(SESSION_MODE_ENV))
        ) or DEFAULT_SESSION_MODE
        self._catalog_ttl = (
            catalog_ttl_seconds
            if catalog_ttl_seconds is not None
            else _float_env(
                os.environ.get(CATALOG_TTL_ENV), DEFAULT_CATALOG_TTL_SECONDS
            )
        )
        self._handshake_timeout = handshake_timeout_seconds
        self._max_frame_bytes = max_frame_bytes
        self._grace_seconds = grace_seconds
        self._active: dict[str, _RunState] = {}
        self._catalog_cache: tuple[float, list[ModelDescriptor]] | None = None

    # ------------------------------------------------------------- manifest

    @property
    def manifest(self) -> DriverManifest:
        return DriverManifest(
            driver_id="devin",
            name="Devin CLI (devin acp) ACP driver",
            version="0.1.0",
            sdk_version=SDK_VERSION,
            protocol_family="acp",
            supported_transports=[TransportKind.ACP],
            synthetic=False,
        )

    def capabilities(self) -> Capabilities:
        """Declared capability matrix - unimplemented features stay false.

        ``workspace_write`` is true because the pinned ``accept-edits`` session
        mode auto-approves workspace edits; ``sessions`` is ``none`` because
        load/resume is deliberately not exposed in this slice even though the
        agent advertises ``loadSession``. Usage is ``unknown``: the ACP
        ``usage_update`` notification reports context occupancy, not metered
        billing, and is never surfaced as token usage.
        """
        return Capabilities(
            streaming=StreamingMode.NATIVE,
            sessions=SessionMode.NONE,
            roles=RoleMode.SERIALIZED,
            structured_output=StructuredOutputMode.NONE,
            external_tool_calls=False,
            internal_tools=True,
            vision=False,
            workspace_write=True,
            web_search=False,
            usage=UsageProvenance.UNKNOWN,
        )

    # ----------------------------------------------------------------- argv

    def _acp_argv(self, model: str | None = None) -> list[str]:
        argv = ["env"]
        for name in SANITIZED_ENV:
            argv += ["-u", name]
        argv += [
            self._cli,
            "--permission-mode",
            ARGV_PERMISSION_MODE,
            "acp",
            "--model",
            model or self._model,
        ]
        return argv

    def _sanitized_argv(self, tail: list[str]) -> list[str]:
        argv = ["env"]
        for name in SANITIZED_ENV:
            argv += ["-u", name]
        return argv + [self._cli] + tail

    def _event_kwargs(self, request: NormalizedRequest, state: _RunState) -> dict[str, Any]:
        return {
            "run_id": request.run_id,
            "sequence": state.next_sequence(),
            "timestamp": _utcnow(),
        }

    def _failed(
        self, request: NormalizedRequest, state: _RunState, code: str, message: str
    ) -> RunFailedEvent:
        return RunFailedEvent(
            **self._event_kwargs(request, state),
            payload=RunFailedPayload(code=code, message=message),
        )

    # ---------------------------------------------------------------- probe

    async def _spawned_stdout(
        self, ctx: RuntimeContext, argv: list[str], timeout: float, cap: int
    ) -> tuple[bytes | None, NdjsonProcessTransport]:
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

    async def probe(self, ctx: RuntimeContext) -> ProbeReport:
        notes = [
            "ACP JSON-RPC over stdio (protocolVersion 1); initialize + session/new probe only, no prompt",
            "answer text is only agent_message_chunk; thought chunks/tool logs/stderr are never forwarded",
            "agentInfo.version is a build string, never the distribution version",
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
            output, transport = await self._spawned_stdout(
                ctx, self._sanitized_argv(["--version"]), VERSION_TIMEOUT_SECONDS, 4096
            )
        except ProcessStartError as exc:
            return ProbeReport(
                ok=False,
                driver_id=self.manifest.driver_id,
                driver_version=self.manifest.version,
                cli_version=None,
                capabilities=self.capabilities(),
                notes=notes + [f"CLI not startable: {exc}"],
            )
        confirmed = await transport.aclose()

        text = (output or b"").decode("utf-8", "replace")
        match = _VERSION_RE.search(text)
        cli_version = match.group(0) if match else None
        notes.append(f"version exit confirmed: {confirmed}")
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

        # Bounded initialize handshake: proves the ACP transport actually works.
        try:
            process = await ctx.executor.spawn(self._acp_argv())
        except ProcessStartError as exc:
            return ProbeReport(
                ok=False,
                driver_id=self.manifest.driver_id,
                driver_version=self.manifest.version,
                cli_version=cli_version,
                capabilities=self.capabilities(),
                notes=notes + [f"ACP server not startable: {exc}"],
            )
        transport = NdjsonProcessTransport(
            process,
            max_frame_bytes=self._max_frame_bytes,
            grace_seconds=self._grace_seconds,
        )
        try:
            await asyncio.wait_for(
                transport.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": ACP_PROTOCOL_VERSION,
                            "clientCapabilities": {},
                            "clientInfo": CLIENT_INFO,
                        },
                    }
                ),
                timeout=VERSION_TIMEOUT_SECONDS,
            )
            deadline = time.monotonic() + VERSION_TIMEOUT_SECONDS
            response = None
            while time.monotonic() < deadline:
                try:
                    frame = await transport.recv_within(
                        max(deadline - time.monotonic(), 0.05)
                    )
                except (MalformedFrame, FrameTooLarge, asyncio.TimeoutError):
                    break
                if frame is None:
                    break
                if frame.get("id") == 1:
                    response = frame
                    break
            if response is None:
                notes.append("ACP initialize returned no response within bound")
            elif "error" in response:
                notes.append("ACP initialize returned a JSON-RPC error")
            elif response.get("result", {}).get("protocolVersion") != ACP_PROTOCOL_VERSION:
                notes.append(
                    "ACP initialize returned an unsupported protocol version"
                )
            else:
                agent_info = response.get("result", {}).get("agentInfo", {})
                notes.append(
                    "ACP initialize ok; agentInfo "
                    f"{agent_info.get('name')!r} build {agent_info.get('version')!r} "
                    "(not the distribution version)"
                )
                return ProbeReport(
                    ok=True,
                    driver_id=self.manifest.driver_id,
                    driver_version=self.manifest.version,
                    cli_version=cli_version,
                    capabilities=self.capabilities(),
                    notes=notes,
                )
        finally:
            await transport.aclose()
        return ProbeReport(
            ok=False,
            driver_id=self.manifest.driver_id,
            driver_version=self.manifest.version,
            cli_version=cli_version,
            capabilities=self.capabilities(),
            notes=notes,
        )

    # ------------------------------------------------------------- discovery

    def _model_allowed(self, model_id: str) -> bool:
        """Operator allowlist: ``DEVIN_MODELS`` list, ``*``, or the legacy pin."""
        return self._allow_all_models or model_id in self._allowed_models

    def _executable(self, model_id: str, cost_tier: Any) -> bool:
        """Discovery never authorizes: allowlist + admitted cost tier required."""
        return (
            self._model_allowed(model_id)
            and isinstance(cost_tier, str)
            and cost_tier in self._allowed_cost_tiers
        )

    async def discover_models(self, ctx: RuntimeContext) -> list[ModelDescriptor]:
        """List the real catalog, verified by exact ``model_uid`` membership.

        ``devin models list --format json`` is the operator-facing catalog; the
        match is on the exact ``model_uid`` only - never a family slug, alias
        or fuzzy selector. Every observed variant becomes a descriptor; the
        descriptor's ``executable`` flag is the operator policy decision
        (allowlist + cost tier), kept strictly separate from verification.
        Results are cached for a bounded, operator-configured TTL so a stale
        ``Free`` tier is not replayed as a pricing claim forever.
        """
        if (
            self._catalog_cache is not None
            and time.monotonic() - self._catalog_cache[0] < self._catalog_ttl
        ):
            return self._catalog_cache[1]

        descriptors = await self._catalog_descriptors(ctx)
        self._catalog_cache = (time.monotonic(), descriptors)
        return descriptors

    async def _catalog_descriptors(self, ctx: RuntimeContext) -> list[ModelDescriptor]:
        source = "devin models list --format json"

        def fallback(
            status: VerificationStatus, reason: str, display: str | None = None
        ) -> list[ModelDescriptor]:
            return [
                ModelDescriptor(
                    model_id=self._model,
                    display_name=display or f"{self._model} ({status.value})",
                    verification=Verification(
                        status=status, source=source, reason=reason
                    ),
                    executable=False,
                )
            ]

        if not self._model_allowed(self._model):
            return fallback(
                VerificationStatus.FAILED,
                f"operator-pinned model {self._model!r} is not in the "
                "execution allowlist",
            )
        if ctx.executor is None:
            return fallback(
                VerificationStatus.UNKNOWN,
                "no process executor supplied; catalog not read",
            )
        try:
            output, transport = await self._spawned_stdout(
                ctx,
                self._sanitized_argv(["models", "list", "--format", "json"]),
                VERSION_TIMEOUT_SECONDS,
                self._max_frame_bytes,
            )
        except ProcessStartError:
            return fallback(VerificationStatus.UNKNOWN, "CLI not startable")
        confirmed = await transport.aclose()
        # A nonzero exit or unconfirmed termination is never trusted data.
        if not confirmed or transport.returncode not in (0, None):
            return fallback(
                VerificationStatus.UNKNOWN,
                "catalog command did not exit cleanly; output not trusted",
            )
        if output is None:
            return fallback(
                VerificationStatus.UNKNOWN, "catalog read timed out or failed"
            )
        try:
            catalog = json.loads(output.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            catalog = None
        families = catalog.get("families") if isinstance(catalog, dict) else None
        if not isinstance(families, list):
            return fallback(
                VerificationStatus.UNKNOWN,
                "catalog output could not be read or parsed",
            )

        descriptors: list[ModelDescriptor] = []
        configured_seen = False
        for family in families:
            if not isinstance(family, dict):
                continue
            family_id = (
                family.get("slug")
                or family.get("family_uid")
                or family.get("family_label")
            )
            aliases = [
                a for a in (family.get("aliases") or []) if isinstance(a, str)
            ]
            for variant in family.get("variants") or []:
                if not isinstance(variant, dict):
                    continue
                uid = variant.get("model_uid")
                if not isinstance(uid, str) or not uid:
                    continue
                tier = variant.get("cost_tier")
                status = VerificationStatus.PASSED
                reason = (
                    "exact model_uid membership verified against the local "
                    f"catalog; valid for {self._catalog_ttl:.0f}s "
                    "(membership only - not an inference or quota claim)"
                )
                if uid == self._model:
                    configured_seen = True
                    if tier != self._expected_cost_tier:
                        # The operator-pinned expectation on the configured
                        # model is a verification failure, not just a policy
                        # denial: a silently more expensive pinned model must
                        # never verify.
                        status = VerificationStatus.FAILED
                        reason = (
                            f"catalog cost_tier {tier!r} does not match the "
                            f"operator-pinned expectation "
                            f"{self._expected_cost_tier!r}"
                        )
                descriptors.append(
                    ModelDescriptor(
                        model_id=uid,
                        display_name=str(variant.get("label") or uid),
                        verification=Verification(
                            status=status, source=source, reason=reason
                        ),
                        cost_tier=tier if isinstance(tier, str) else None,
                        family=family_id if isinstance(family_id, str) else None,
                        aliases=aliases,
                        executable=(
                            status is VerificationStatus.PASSED
                            and self._executable(uid, tier)
                        ),
                    )
                )
        if not configured_seen:
            descriptors.append(
                ModelDescriptor(
                    model_id=self._model,
                    display_name=f"{self._model} (catalog verification failed)",
                    verification=Verification(
                        status=VerificationStatus.FAILED,
                        source=source,
                        reason=(
                            "model_uid not present in catalog "
                            "(exact match required)"
                        ),
                    ),
                    executable=False,
                )
            )
        return descriptors

    # -------------------------------------------------------------- execute

    def _resolve_run_model(
        self, request: NormalizedRequest
    ) -> tuple[str | None, tuple[str, str] | None]:
        """Resolve the exact model this run may use, or a rejection.

        Selection comes from the admitted request (``model_alias``), falling
        back to the operator-pinned default only when the request carries no
        model. ``self._model`` is never mutated per run. Catalog membership and
        cost-tier admission are re-verified by the caller against the bounded
        TTL cache before any subprocess exists.
        """
        if request.reasoning_effort is not None:
            return None, (
                "unsupported_effort",
                "the Devin catalog exposes no per-model effort option; an "
                "explicit reasoning_effort is rejected rather than silently "
                "dropped",
            )
        model = request.model_alias or self._model
        if (
            request.resolved_model is not None
            and request.resolved_model != model
        ):
            return None, (
                "resolved_model_mismatch",
                f"admitted resolved model {request.resolved_model!r} does not "
                f"match the requested {model!r}",
            )
        if not self._model_allowed(model):
            return None, (
                "unsupported_model",
                f"model {model!r} is not in the operator execution allowlist",
            )
        return model, None

    @staticmethod
    def _serialize_messages(request: NormalizedRequest) -> str:
        return "\n\n".join(
            f"{message.role}: {message.content}" for message in request.messages
        )

    async def execute(
        self, request: NormalizedRequest, ctx: RuntimeContext
    ) -> AsyncIterator[RunEvent]:
        state = _RunState()

        def fail(code: str, message: str) -> RunFailedEvent:
            return self._failed(request, state, code, message)

        # ------------------------------------------------ preflight (no spawn)
        if ctx.executor is None:
            yield fail(
                "no_process_executor",
                "the runner supplied no process executor; refusing to run",
            )
            return
        if not request.deadline_seconds or request.deadline_seconds <= 0:
            yield fail("no_deadline", "a finite deadline is required to run the CLI")
            return
        model, rejection = self._resolve_run_model(request)
        if rejection is not None:
            yield fail(*rejection)
            return
        cwd = (
            ctx.workspace.root
            if ctx.workspace is not None
            else self._workspace_root
        )
        if not cwd:
            yield fail(
                "no_workspace",
                "no workspace service or operator workspace root configured",
            )
            return

        # Catalog re-verification on every run: the bounded-TTL cache is
        # consulted again so a stale `Free` membership is never replayed as a
        # standing authorization. The check itself is only a read-only
        # `models list` spawn — never an agent prompt — and an absent,
        # non-admitted, expired, or unreadable catalog fails the run before the
        # agent subprocess exists.
        descriptor = next(
            (
                d
                for d in await self.discover_models(ctx)
                if d.model_id == model
            ),
            None,
        )
        if (
            descriptor is None
            or descriptor.verification.status is not VerificationStatus.PASSED
            or not descriptor.executable
        ):
            detail = (
                descriptor.verification.reason
                if descriptor is not None
                else "model is absent from the verified catalog"
            )
            yield fail(
                "catalog_not_verified",
                f"catalog admission for {model!r} failed: {detail}; "
                "refusing to spawn the agent",
            )
            return
        state.expected_model = model

        # run.started is emitted here — after every admission gate and
        # immediately before the ACP agent subprocess is spawned — so it
        # precedes the first effectful action of the run. A failed spawn
        # still produces started -> failed rather than silence.
        yield RunStartedEvent(
            **self._event_kwargs(request, state),
            payload=RunStartedPayload(
                preset=request.preset,
                model_alias=model,
                reasoning_effort=request.reasoning_effort,
                resolved_model=request.resolved_model,
            ),
        )

        try:
            process = await ctx.executor.spawn(self._acp_argv(model), cwd=cwd)
        except ProcessStartError as exc:
            yield fail("cli_not_startable", str(exc))
            return

        transport = NdjsonProcessTransport(
            process,
            max_frame_bytes=self._max_frame_bytes,
            grace_seconds=self._grace_seconds,
        )
        state.transport = transport
        self._active[request.run_id] = state
        deadline_at = time.monotonic() + request.deadline_seconds
        terminal_sent = False

        try:
            phase_deadline = min(
                deadline_at, time.monotonic() + self._handshake_timeout
            )

            async def rpc(method: str, params: dict[str, Any]):
                """Send one request and pump until its response arrives."""
                request_id = state.next_rpc_id()
                state.awaiting = request_id
                state.response = None
                await asyncio.wait_for(
                    transport.send(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": method,
                            "params": params,
                        }
                    ),
                    timeout=max(phase_deadline - time.monotonic(), 0.001),
                )
                async for event in self._pump(
                    request, ctx, transport, state, deadline_at,
                    phase_deadline=phase_deadline,
                    done=lambda s: s.response is not None,
                ):
                    yield event
                state.awaiting = None

            # initialize
            async for event in rpc(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientCapabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            ):
                yield event
            init = state.response
            if not isinstance(init, dict) or init.get("protocolVersion") != ACP_PROTOCOL_VERSION:
                yield fail("protocol_error", "initialize did not confirm protocolVersion 1")
                terminal_sent = True
                return

            # session/new
            state.response = None
            async for event in rpc(
                "session/new", {"cwd": cwd, "mcpServers": []}
            ):
                yield event
            session = state.response
            if not isinstance(session, dict) or not session.get("sessionId"):
                yield fail("protocol_error", "session/new returned no sessionId")
                terminal_sent = True
                return
            state.session_id = str(session["sessionId"])

            modes = session.get("modes") or {}
            state.current_mode = modes.get("currentModeId")
            available = {
                entry.get("id")
                for entry in modes.get("availableModes") or []
                if isinstance(entry, dict)
            }
            model_value = self._session_model_value(session)
            if model_value is None:
                yield fail(
                    "model_mismatch",
                    "session/new did not expose a model config currentValue to verify",
                )
                terminal_sent = True
                return
            state.model_value = model_value
            if model_value != model:
                yield fail(
                    "model_mismatch",
                    f"session model currentValue {model_value!r} != pinned {model!r}",
                )
                terminal_sent = True
                return

            # mode handling: set_mode + acknowledged current_mode_update
            if self._session_mode not in available:
                yield fail(
                    "unsupported_mode",
                    f"configured session mode {self._session_mode!r} is not in the "
                    "session's availableModes",
                )
                terminal_sent = True
                return
            # Entering or staying in bypass requires an explicit injected
            # permission policy; it is never the default merely because
            # prompting for approval is inconvenient.
            if self._session_mode == BYPASS_MODE and not (
                ctx.permissions is not None and ctx.permissions.allows(BYPASS_ACTION)
            ):
                yield fail(
                    "mode_not_authorized",
                    "session mode 'bypass' requires an explicit operator "
                    "permission policy; none authorized it",
                )
                terminal_sent = True
                return
            if state.current_mode != self._session_mode:
                state.response = None
                async for event in rpc(
                    "session/set_mode",
                    {"sessionId": state.session_id, "modeId": self._session_mode},
                ):
                    yield event
                # The {} result is not the acknowledgement: the mode only counts
                # once session/update current_mode_update confirms it.
                state.awaiting_mode = self._session_mode
                try:
                    async for event in self._pump(
                        request, ctx, transport, state, deadline_at,
                        phase_deadline=phase_deadline,
                        done=lambda s: s.current_mode == self._session_mode,
                    ):
                        yield event
                except _ModeAckFailed:
                    yield fail(
                        "mode_ack_mismatch",
                        "session/update acknowledged a different mode than requested",
                    )
                    terminal_sent = True
                    return
                except _PhaseTimeout:
                    yield fail(
                        "mode_ack_timeout",
                        "no current_mode_update acknowledgement before the bounded wait expired",
                    )
                    terminal_sent = True
                    return
                finally:
                    state.awaiting_mode = None

            await self._record_facts(ctx, request, state)

            # session/prompt - bounded by the run deadline only
            state.response = None
            prompt_id = state.next_rpc_id()
            state.awaiting = prompt_id
            await asyncio.wait_for(
                transport.send(
                    {
                        "jsonrpc": "2.0",
                        "id": prompt_id,
                        "method": "session/prompt",
                        "params": {
                            "sessionId": state.session_id,
                            "prompt": [
                                {
                                    "type": "text",
                                    "text": self._serialize_messages(request),
                                }
                            ],
                        },
                    }
                ),
                timeout=max(deadline_at - time.monotonic(), 0.001),
            )
            async for event in self._pump(
                request, ctx, transport, state, deadline_at,
                phase_deadline=None,
                done=lambda s: s.response is not None,
            ):
                yield event
            state.awaiting = None

            result = state.response or {}
            stop = result.get("stopReason") if isinstance(result, dict) else None
            if stop == "end_turn":
                yield RunCompletedEvent(
                    **self._event_kwargs(request, state),
                    payload=RunCompletedPayload(
                        outcome="partial" if state.denied else "succeeded",
                        usage=Usage(provenance=UsageProvenance.UNKNOWN),
                        message=(
                            "turn ended; at least one permission request was denied"
                            if state.denied
                            else "turn ended; no verification evidence collected"
                        ),
                    ),
                )
            elif stop in STOP_PARTIAL:
                yield RunCompletedEvent(
                    **self._event_kwargs(request, state),
                    payload=RunCompletedPayload(
                        outcome="partial",
                        usage=Usage(provenance=UsageProvenance.UNKNOWN),
                        message=f"turn ended with stopReason {stop!r}; work may be incomplete",
                    ),
                )
            elif stop == "cancelled":
                yield RunCancelledEvent(
                    **self._event_kwargs(request, state),
                    payload=RunCancelledPayload(
                        reason="the agent reported a cancelled stop reason"
                    ),
                )
            else:
                yield fail(
                    "protocol_error",
                    f"session/prompt returned an undocumented stopReason {stop!r}",
                )
            terminal_sent = True
            return
        except _Cancelled:
            for event in await self._forced_stop(
                request, ctx, transport, state, deadline_at, "cancelled",
                "cancellation requested",
            ):
                yield event
                terminal_sent = True
            return
        except _DeadlineExceeded:
            for event in await self._forced_stop(
                request, ctx, transport, state, deadline_at, "deadline",
                "run deadline expired",
            ):
                yield event
                terminal_sent = True
            return
        except _PhaseTimeout as exc:
            for event in await self._forced_stop(
                request, ctx, transport, state, deadline_at, exc.code,
                "a handshake phase exceeded its bounded wait",
            ):
                yield event
                terminal_sent = True
            return
        except _RpcError:
            for event in await self._forced_stop(
                request, ctx, transport, state, deadline_at, "rpc_error",
                "the agent returned a JSON-RPC error for a client request",
            ):
                yield event
                terminal_sent = True
            return
        except _ProtocolError as exc:
            for event in await self._forced_stop(
                request, ctx, transport, state, deadline_at, exc.code,
                "the agent emitted a frame outside the documented protocol",
            ):
                yield event
                terminal_sent = True
            return
        except (MalformedFrame, FrameTooLarge) as exc:
            for event in await self._forced_stop(
                request, ctx, transport, state, deadline_at, "protocol_error",
                f"invalid CLI frame: {exc}",
            ):
                yield event
                terminal_sent = True
            return
        except _Eof:
            for event in await self._forced_stop(
                request, ctx, transport, state, deadline_at, "missing_result",
                "the CLI stream ended before the awaited response",
            ):
                yield event
                terminal_sent = True
            return
        except (OSError, asyncio.TimeoutError) as exc:
            for event in await self._forced_stop(
                request, ctx, transport, state, deadline_at, "transport_error",
                f"stdio transport failed: {type(exc).__name__}",
            ):
                yield event
                terminal_sent = True
            return
        finally:
            self._active.pop(request.run_id, None)
            await transport.aclose()
            if not terminal_sent:
                # Leaving without a terminal event is intentional only for an
                # unconfirmed termination; the runner then records unknown.
                ctx.logger.warning(
                    "devin run ended without a terminal event",
                    run_id=request.run_id,
                )

    async def _record_facts(
        self, ctx: RuntimeContext, request: NormalizedRequest, state: _RunState
    ) -> None:
        facts = {
            "session_id": state.session_id,
            "model": state.model_value,
            "mode": state.current_mode,
        }
        state.facts_recorded = True
        try:
            await ctx.session_store.put(f"devin/{request.run_id}", facts)
        except Exception:  # noqa: BLE001 - recording must not break the run
            pass
        ctx.logger.info(
            "devin session established",
            run_id=request.run_id,
            session_id=state.session_id,
            model=state.model_value,
            mode=state.current_mode,
        )

    # ---------------------------------------------------- pump / dispatch

    async def _pump(
        self,
        request: NormalizedRequest,
        ctx: RuntimeContext,
        transport: NdjsonProcessTransport,
        state: _RunState,
        deadline_at: float,
        *,
        phase_deadline: float | None,
        done,
    ) -> AsyncIterator[RunEvent]:
        """Read frames until ``done(state)``; dispatch everything else."""
        while not done(state):
            if ctx.cancellation.is_requested() or state.cancel_event.is_set():
                raise _Cancelled()
            now = time.monotonic()
            if now >= deadline_at:
                raise _DeadlineExceeded()
            if phase_deadline is not None and now >= phase_deadline:
                raise _PhaseTimeout("handshake_timeout")
            bound = deadline_at if phase_deadline is None else min(deadline_at, phase_deadline)
            try:
                frame = await transport.recv_within(min(bound - now, POLL_SECONDS))
            except asyncio.TimeoutError:
                continue
            if frame is None:
                raise _Eof()
            async for event in self._dispatch(request, transport, state, frame):
                yield event

    async def _dispatch(
        self,
        request: NormalizedRequest,
        transport: NdjsonProcessTransport,
        state: _RunState,
        frame: dict[str, Any],
    ) -> AsyncIterator[RunEvent]:
        has_method = isinstance(frame.get("method"), str)
        if frame.get("id") == state.awaiting and (
            "result" in frame or "error" in frame
        ):
            error = frame.get("error")
            if isinstance(error, dict):
                raise _RpcError(str(error.get("code")))
            state.response = frame.get("result")
            return
        if has_method and "id" in frame:
            async for event in self._handle_agent_request(
                request, transport, state, frame
            ):
                yield event
            return
        if has_method:
            async for event in self._handle_notification(request, state, frame):
                yield event
            return
        if "id" in frame:
            return  # a response for an id we are not tracking; ignored boundedly
        raise _ProtocolError("protocol_error")

    async def _handle_agent_request(
        self,
        request: NormalizedRequest,
        transport: NdjsonProcessTransport,
        state: _RunState,
        frame: dict[str, Any],
    ) -> AsyncIterator[RunEvent]:
        method = frame.get("method")
        if method == "session/request_permission":
            params = frame.get("params") or {}
            tool_call = params.get("toolCall") or {}
            action = (
                tool_call.get("title")
                or tool_call.get("kind")
                or str(tool_call.get("toolCallId") or "tool")
            )
            yield PermissionRequiredEvent(
                **self._event_kwargs(request, state),
                payload=PermissionRequiredPayload(
                    request_id=str(frame.get("id")), action=str(action)[:120]
                ),
            )
            state.denied = True
            outcome = self._deny_outcome(params.get("options") or [])
            try:
                await asyncio.wait_for(
                    transport.send(
                        {
                            "jsonrpc": "2.0",
                            "id": frame.get("id"),
                            "result": {"outcome": outcome},
                        }
                    ),
                    timeout=5.0,
                )
            except (OSError, asyncio.TimeoutError):
                pass
            return
        # Every other agent->client method (fs/*, terminal/*, authenticate,
        # cognition-private RPCs, ...) is unsupported in this slice: reply with
        # a standard JSON-RPC method-not-found error instead of hanging the turn.
        try:
            await asyncio.wait_for(
                transport.send(
                    {
                        "jsonrpc": "2.0",
                        "id": frame.get("id"),
                        "error": {
                            "code": -32601,
                            "message": "method not supported by this client",
                        },
                    }
                ),
                timeout=5.0,
            )
        except (OSError, asyncio.TimeoutError):
            pass

    @staticmethod
    def _deny_outcome(options: list[Any]) -> dict[str, Any]:
        for option in options:
            if isinstance(option, dict) and option.get("kind") in (
                "reject_once",
                "reject_always",
            ) and option.get("optionId"):
                return {"outcome": "selected", "optionId": option["optionId"]}
        return {"outcome": "cancelled"}

    async def _handle_notification(
        self,
        request: NormalizedRequest,
        state: _RunState,
        frame: dict[str, Any],
    ) -> AsyncIterator[RunEvent]:
        if frame.get("method") != "session/update":
            return  # other notifications (incl. cognition-private) ignored boundedly
        params = frame.get("params") or {}
        update = params.get("update") or {}
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                text = content.get("text")
                if isinstance(text, str) and text:
                    yield MessageDeltaEvent(
                        **self._event_kwargs(request, state),
                        payload=MessageDeltaPayload(text=text),
                    )
            return
        if kind == "tool_call":
            tool_call_id = str(update.get("toolCallId") or "tool")
            yield ToolStartedEvent(
                **self._event_kwargs(request, state),
                payload=ToolStartedPayload(
                    tool_call_id=tool_call_id,
                    name=str(update.get("title") or update.get("name") or "tool"),
                ),
            )
            status = update.get("status")
            if status in ("completed", "failed"):
                yield ToolCompletedEvent(
                    **self._event_kwargs(request, state),
                    payload=ToolCompletedPayload(
                        tool_call_id=tool_call_id, status=status
                    ),
                )
            return
        if kind == "tool_call_update":
            tool_call_id = str(update.get("toolCallId") or "tool")
            status = update.get("status")
            if status in ("completed", "failed"):
                yield ToolCompletedEvent(
                    **self._event_kwargs(request, state),
                    payload=ToolCompletedPayload(
                        tool_call_id=tool_call_id, status=status
                    ),
                )
            for block in update.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "diff":
                    path = block.get("path")
                    if isinstance(path, str) and path:
                        yield ArtifactCreatedEvent(
                            **self._event_kwargs(request, state),
                            payload=ArtifactCreatedPayload(
                                artifact_id=path, kind="diff"
                            ),
                        )
            return
        if kind == "current_mode_update":
            mode = update.get("currentModeId")
            if isinstance(mode, str):
                if state.awaiting_mode is not None and mode != state.awaiting_mode:
                    raise _ModeAckFailed()
                state.current_mode = mode
            return
        if kind == "config_option_update":
            option_id = update.get("configId") or update.get("id")
            value = update.get("value", update.get("currentValue"))
            if (
                option_id == "model"
                and isinstance(value, str)
                and value != getattr(state, "expected_model", self._model)
            ):
                raise _ProtocolError("model_changed")
            if option_id in ("mode", "session_mode") and isinstance(value, str):
                state.current_mode = value
            return
        # agent_thought_chunk, user_message_chunk, plan, usage_update,
        # available_commands_update, session_info_update and anything
        # undocumented are ignored boundedly - none of it is answer text.

    @staticmethod
    def _session_model_value(session: dict[str, Any]) -> str | None:
        """The acknowledged model for the new session, or None when unverifiable."""
        for option in session.get("configOptions") or []:
            if not isinstance(option, dict):
                continue
            if option.get("id") == "model" or option.get("category") == "model":
                value = option.get("currentValue") or option.get("value")
                return value if isinstance(value, str) else None
        models = session.get("models")
        if isinstance(models, dict):
            value = models.get("currentModelId")
            return value if isinstance(value, str) else None
        return None

    async def _forced_stop(
        self,
        request: NormalizedRequest,
        ctx: RuntimeContext,
        transport: NdjsonProcessTransport,
        state: _RunState,
        deadline_at: float,
        code: str,
        message: str,
    ) -> list[RunEvent]:
        stopping = ctx.cancellation.is_requested() or state.cancel_event.is_set()
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
                    **self._event_kwargs(request, state),
                    payload=RunCancelledPayload(reason=reason),
                )
            ]
        return [
            RunFailedEvent(
                **self._event_kwargs(request, state),
                payload=RunFailedPayload(code=code, message=message),
            )
        ]

    # ---------------------------------------------------------------- cancel

    async def cancel(self, run_id: str, ctx: RuntimeContext) -> CancelResult:
        now = _utcnow()
        state = self._active.get(run_id)
        if state is None or state.transport is None:
            return CancelResult(
                run_id=run_id,
                requested=True,
                requested_at=now,
                confirmed=False,
                confirmed_at=None,
                deadline_seconds=1.0,
                detail="no active CLI process for this run",
            )
        transport = state.transport
        # Best-effort protocol cancel first; a session/cancel acknowledgement is
        # NOT proof of stop, so confirmation below comes only from termination.
        if state.session_id is not None:
            try:
                await asyncio.wait_for(
                    transport.send(
                        {
                            "jsonrpc": "2.0",
                            "method": "session/cancel",
                            "params": {"sessionId": state.session_id},
                        }
                    ),
                    timeout=CANCEL_NOTIFY_SECONDS,
                )
            except (OSError, asyncio.TimeoutError):
                pass
            # Give the agent a bounded window to answer the in-flight turn with
            # a cancelled stopReason before the run loop is told to force-stop;
            # terminating first could kill it before the notification is read.
            grace_at = time.monotonic() + CANCEL_TURN_GRACE_SECONDS
            while (
                state.response is None
                and not transport.exited()
                and time.monotonic() < grace_at
            ):
                await asyncio.sleep(POLL_SECONDS)
        state.cancel_event.set()
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
            confirmed_at=_utcnow() if confirmed else None,
            deadline_seconds=1.0,
            detail=detail,
        )

    async def aclose(self) -> None:
        for state in list(self._active.values()):
            if state.transport is not None:
                await state.transport.terminate()
        self._active.clear()
