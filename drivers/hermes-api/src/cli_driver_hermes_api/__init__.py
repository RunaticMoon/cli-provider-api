"""Hermes-agent CLI ProviderDriver for B.AI / CommandCode official model APIs.

One driver, two operator presets. A run request carries only a public preset
alias; the driver pins exactly one official backend (base URL + provider profile
+ exact model id + key env name) for the whole Hermes run. There is no inner
fallback, no provider reclassification and no tier logic here.

Verified interface facts (Hermes ``v0.21.3``, upstream ``d86a1687``):

* ``hermes chat --query-file PATH --oneshot --provider NAME --model ID
  --reasoning LEVEL --toolsets file,terminal --format stream-json --in DIR
  --max-turns N --run-budget SECONDS --yolo --ignore-rules`` runs the whole
  agent tool loop internally and speaks NDJSON on stdout;
* the output envelope is discriminated by ``type``: ``system/init`` first,
  ``text`` deltas, ``tool_use`` / ``tool_result`` and exactly one terminal
  ``result`` record (``hermes_cli/stream_json.py``);
* only ``type == "text"`` records are model-visible answer text. Tool envelopes
  and the terminal record's ``text`` are never forwarded as answer deltas;
* a named ``providers.<name>`` entry in the task-local config resolves to the
  generic ``custom`` profile (top-level ``reasoning_effort``); the bundled
  ``commandcode`` profile additionally emits DeepSeek ``thinking`` controls;
* ``--safe-mode`` / ``--ignore-user-config`` would bypass the generated
  task-local config and are therefore never passed.

Isolation: every run writes a fresh task-local ``HERMES_HOME`` (0700) holding a
JSON-emitted ``config.yaml`` (a YAML-1.2 subset, no interpolation) and the query
file (0600). The API key reaches the native client via the operator environment
only — the config carries the env var *name*, never the value, and no secret is
ever placed in argv, prompts, logs or result payloads.
"""

from __future__ import annotations

import asyncio
import dataclasses
import http.client
import json
import os
import re
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

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
)

CLI_ENV = "HERMES_API_CLI"
REASONING_ENV = "HERMES_API_REASONING"
MAX_TURNS_ENV = "HERMES_API_MAX_TURNS"
RUN_BUDGET_ENV = "HERMES_API_RUN_BUDGET"
STATE_DIR_ENV = "HERMES_API_STATE_DIR"
EXPECTED_VERSION_ENV = "HERMES_API_EXPECTED_VERSION"
CATALOG_TTL_ENV = "HERMES_API_CATALOG_TTL"
CATALOG_TIMEOUT_ENV = "HERMES_API_CATALOG_TIMEOUT"

DEFAULT_CLI = "hermes"
DEFAULT_REASONING = "low"
DEFAULT_MAX_TURNS = 40
DEFAULT_RUN_BUDGET_CAP = 600
VERSION_TIMEOUT_SECONDS = 10.0
DEFAULT_CATALOG_TTL_SECONDS = 300.0
DEFAULT_CATALOG_TIMEOUT_SECONDS = 10.0
CATALOG_MAX_BYTES = 65536
POLL_SECONDS = 0.25

#: Permission-policy action required before ``--yolo`` is passed. The runtime
#: context's permission service must allow it, or the run fails before spawn.
YOLO_ACTION = "hermes.yolo"

#: Reasoning values verified on both official APIs (B.AI docs / CommandCode
#: profile wire mapping). Internal effort hints (auto/economy/balanced/…) and
#: unverified levels (medium/xhigh/…) are rejected before any process spawn.
REASONING_LEVELS = frozenset({"low", "high", "max"})

KNOWN_TYPES = frozenset({"system", "text", "tool_use", "tool_result", "result"})

_VERSION_RE = re.compile(r"v?(\d+\.\d+(?:\.\d+)?)")
# Secret-shaped tokens are scrubbed from provider/driver messages before they
# can reach an event payload. Key *values* are also removed when resolvable.
_SECRET_RE = re.compile(
    r"(?i)\b(?:sk|key|token|secret|bearer|api[-_]?key)[-_][A-Za-z0-9._-]{6,}\b"
)
_MAX_ERROR_CHARS = 240

# Env names matching these are stripped from the spawned CLI's environment:
# only the *selected* preset's key env survives. This keeps an unrelated
# provider's key — or a Lead profile's credentials — out of a child that only
# needs one backend. Best-effort hygiene, not a sandbox: values never appear
# in argv (only names are unset via `env -u`).
_SECRET_NAME_RE = re.compile(
    r"(?i)(API[-_]?KEY|TOKEN|SECRET|PASSW|CREDENTIAL|BEARER|KEYRING|"
    r"PRIVATE[-_]?KEY|AUTH|COOKIE|_JWT)"
)
_SCRUB_PREFIXES = (
    "DEVIN_",
    "CODEX_",
    "HERMES_",
    "OPENAI_",
    "OPENROUTER_",
    "ANTHROPIC",
    "CLAUDE",
    "DEEPSEEK",
    "GEMINI",
    "GOOGLE_API",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "MOONSHOT",
    "KIMI",
    "TOGETHER",
    "GROQ",
    "MISTRAL",
    "XAI",
    "QWEN",
    "ZHIPU",
    "BAI_",
    "COMMANDCODE",
    "HF_",
    "HUGGING",
    "COGNITION",
)


@dataclass(frozen=True)
class ProviderPreset:
    """One operator-selectable official-API pin.

    ``provider_kind`` selects the Hermes config shape:

    * ``"custom"`` — a named ``providers.<name>`` entry (generic custom
      profile; ``key_env`` resolves the key from the process environment);
    * ``"builtin"`` — a bundled provider profile (e.g. ``commandcode``);
      the config pins ``model.provider``/``model.base_url`` instead so the
      built-in profile — including its DeepSeek wire handling — stays in charge.

    ``base_path`` is the path component of ``base_url``; tests override
    ``base_url`` with a loopback origin and reuse ``base_path`` unchanged.

    ``base_url_env`` names an *operator* env var that may replace the origin
    (``{override}{base_path}`` becomes the effective ``base_url``). It exists
    for fixture/loopback deployments — like ``HERMES_API_CLI`` it is runner-
    process operator config, never request-derived, and it is scrubbed from
    the child environment by the ``HERMES_`` prefix rule. Production default
    remains the official endpoint.
    """

    alias: str
    provider_name: str
    provider_kind: str
    model_id: str
    base_url: str
    base_path: str
    api_mode: str
    key_env: str
    base_url_env: str | None = None

    @property
    def descriptor_id(self) -> str:
        """SDK-safe model descriptor id (``/`` is not a valid id character)."""
        return f"{self.provider_name}:{self.model_id.rsplit('/', 1)[-1]}"


PRESETS: dict[str, ProviderPreset] = {
    "bai/deepseek-v4.1-flash": ProviderPreset(
        alias="bai/deepseek-v4.1-flash",
        provider_name="bai",
        provider_kind="custom",
        model_id="deepseek-v4.1-flash",
        base_url="https://api.b.ai/v1",
        base_path="/v1",
        api_mode="chat_completions",
        key_env="BAI_API_KEY",
        base_url_env="HERMES_API_BAI_BASE_URL",
    ),
    "commandcode/deepseek-v4.1-flash": ProviderPreset(
        alias="commandcode/deepseek-v4.1-flash",
        provider_name="commandcode",
        provider_kind="builtin",
        model_id="deepseek/deepseek-v4.1-flash",
        base_url="https://api.commandcode.ai/provider/v1",
        base_path="/provider/v1",
        api_mode="chat_completions",
        key_env="COMMANDCODE_API_KEY",
        base_url_env="HERMES_API_COMMANDCODE_BASE_URL",
    ),
}


def _positive_int(raw: Any, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _float_seconds(raw: Any, default: float) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


class HermesApiDriver(BaseDriver):
    """One Hermes process per run, pinned to one official API preset."""

    def __init__(
        self,
        *,
        cli_command: str | None = None,
        reasoning: str | None = None,
        presets: Mapping[str, ProviderPreset] | None = None,
        max_turns: int | None = None,
        run_budget_cap: int | None = None,
        state_dir: str | None = None,
        expected_version: str | None = None,
        catalog_ttl_seconds: float | None = None,
        catalog_timeout_seconds: float | None = None,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    ) -> None:
        self._cli = cli_command or os.environ.get(CLI_ENV) or DEFAULT_CLI
        self._reasoning = (
            reasoning
            if reasoning is not None
            else (os.environ.get(REASONING_ENV) or DEFAULT_REASONING)
        )
        self._presets: dict[str, ProviderPreset] = dict(presets or PRESETS)
        for alias, preset in self._presets.items():
            override_raw = (
                os.environ.get(preset.base_url_env)
                if preset.base_url_env
                else None
            )
            override = (override_raw or "").strip().rstrip("/")
            if override:
                self._presets[alias] = dataclasses.replace(
                    preset, base_url=f"{override}{preset.base_path}"
                )
        self._max_turns = _positive_int(
            max_turns if max_turns is not None else os.environ.get(MAX_TURNS_ENV),
            DEFAULT_MAX_TURNS,
        )
        self._run_budget_cap = _positive_int(
            run_budget_cap
            if run_budget_cap is not None
            else os.environ.get(RUN_BUDGET_ENV),
            DEFAULT_RUN_BUDGET_CAP,
        )
        self._state_dir = state_dir or os.environ.get(STATE_DIR_ENV)
        self._expected_version = (
            expected_version
            if expected_version is not None
            else os.environ.get(EXPECTED_VERSION_ENV)
        )
        self._catalog_ttl = (
            catalog_ttl_seconds
            if catalog_ttl_seconds is not None
            else _float_seconds(
                os.environ.get(CATALOG_TTL_ENV), DEFAULT_CATALOG_TTL_SECONDS
            )
        )
        self._catalog_timeout = (
            catalog_timeout_seconds
            if catalog_timeout_seconds is not None
            else _float_seconds(
                os.environ.get(CATALOG_TIMEOUT_ENV),
                DEFAULT_CATALOG_TIMEOUT_SECONDS,
            )
        )
        self._max_frame_bytes = max_frame_bytes
        self._grace_seconds = grace_seconds
        self._active: dict[str, NdjsonProcessTransport] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._catalog_cache: dict[str, tuple[float, ModelDescriptor]] = {}

    # ------------------------------------------------------------- manifest

    @property
    def manifest(self) -> DriverManifest:
        return DriverManifest(
            driver_id="hermes-api",
            name="Hermes CLI driver for B.AI/CommandCode official APIs",
            version="0.1.0",
            sdk_version=SDK_VERSION,
            protocol_family="hermes-stream-json",
            supported_transports=[TransportKind.STDIO],
            synthetic=False,
        )

    def capabilities(self) -> Capabilities:
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
            usage=UsageProvenance.REPORTED,
        )

    # ---------------------------------------------------------------- probe

    async def probe(self, ctx: RuntimeContext) -> ProbeReport:
        notes = [
            "hermes chat --format stream-json; one process per run",
            "presets pin B.AI or CommandCode official API + exact model",
            "only stream-json 'text' records are treated as answer text",
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
        cli_version = match.group(1) if match else None
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
                notes=notes + [
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

    def _catalog_url(self, preset: ProviderPreset) -> str:
        return f"{preset.base_url}/models"

    def _fetch_catalog_ids(
        self, preset: ProviderPreset, key: str
    ) -> tuple[str, set[str] | None, str | None]:
        """One bounded GET ``{base_url}/models`` with the operator key.

        Synchronous ``http.client`` (stdlib) — run in a thread by the caller.
        Returns ``(kind, ids, detail)`` where ``kind`` is one of ``ok``,
        ``auth``, ``redirect``, ``http_error``, ``network``, ``malformed``,
        ``oversize``. Redirects are never followed, so the credential is never
        forwarded off-origin. ``detail`` carries only canned text + an HTTP
        status code — never response bodies or headers, which could echo the
        key back.
        """
        url = self._catalog_url(preset)
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "invalid_url", None, "catalog endpoint is not http(s)"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        conn_cls = (
            http.client.HTTPSConnection
            if parts.scheme == "https"
            else http.client.HTTPConnection
        )
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        conn = conn_cls(parts.hostname, port, timeout=self._catalog_timeout)
        try:
            conn.request(
                "GET",
                path,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                },
            )
            resp = conn.getresponse()
            status = resp.status
            if status in (301, 302, 303, 307, 308):
                resp.read(256)  # drain a little, then drop the connection
                return "redirect", None, f"HTTP {status}"
            if status in (401, 403):
                resp.read(256)
                return "auth", None, f"HTTP {status}"
            if status != 200:
                resp.read(256)
                return "http_error", None, f"HTTP {status}"
            body = resp.read(CATALOG_MAX_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:
            return "network", None, type(exc).__name__
        finally:
            conn.close()
        if len(body) > CATALOG_MAX_BYTES:
            return "oversize", None, (
                f"catalog body exceeds {CATALOG_MAX_BYTES} bytes"
            )
        try:
            doc = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "malformed", None, "catalog body is not valid JSON"
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, list):
            return "malformed", None, "catalog body carries no model list"
        ids = {
            entry["id"]
            for entry in data
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }
        return "ok", ids, None

    async def _catalog_descriptor(
        self, preset: ProviderPreset
    ) -> ModelDescriptor:
        """Catalog membership for one preset, cached for a bounded TTL."""
        cached = self._catalog_cache.get(preset.alias)
        if (
            cached is not None
            and time.monotonic() - cached[0] < self._catalog_ttl
        ):
            return cached[1]
        descriptor = await self._verify_catalog(preset)
        self._catalog_cache[preset.alias] = (time.monotonic(), descriptor)
        return descriptor

    async def _verify_catalog(self, preset: ProviderPreset) -> ModelDescriptor:
        """Bounded official ``GET /models`` check of the exact pinned id.

        Presence in the catalog is *not* an entitlement claim: quota, billing
        and eligibility are never inferred from it. A missing operator key, an
        unreachable endpoint, a redirect or unreadable body are all reported
        honestly (``unknown``); an authoritative refusal (auth rejection or a
        parsed catalog without the exact id) is ``failed``.
        """
        source = f"GET {self._catalog_url(preset)}"
        key = os.environ.get(preset.key_env)
        if not key:
            return ModelDescriptor(
                model_id=preset.descriptor_id,
                display_name=f"{preset.model_id} via {preset.provider_name}",
                verification=Verification(
                    status=VerificationStatus.UNKNOWN,
                    source=source,
                    reason=(
                        f"operator env {preset.key_env} is not set; the "
                        "provider catalog was not queried"
                    ),
                ),
            )
        try:
            kind, ids, detail = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_catalog_ids, preset, key),
                timeout=self._catalog_timeout + 2.0,
            )
        except asyncio.TimeoutError:
            kind, ids, detail = (
                "network", None, "bounded catalog wait expired",
            )
        if kind == "ok" and ids is not None:
            if preset.model_id in ids:
                return ModelDescriptor(
                    model_id=preset.descriptor_id,
                    display_name=f"{preset.model_id} via {preset.provider_name}",
                    verification=Verification(
                        status=VerificationStatus.PASSED,
                        source=source,
                        reason=(
                            f"exact model id {preset.model_id!r} present in "
                            "the provider catalog; presence is not a quota or "
                            f"entitlement claim; valid for "
                            f"{self._catalog_ttl:.0f}s"
                        ),
                    ),
                )
            return ModelDescriptor(
                model_id=preset.descriptor_id,
                display_name=f"{preset.model_id} via {preset.provider_name}",
                verification=Verification(
                    status=VerificationStatus.FAILED,
                    source=source,
                    reason=(
                        f"exact model id {preset.model_id!r} is not present "
                        "in the provider catalog"
                    ),
                ),
            )
        if kind == "auth":
            return ModelDescriptor(
                model_id=preset.descriptor_id,
                display_name=f"{preset.model_id} via {preset.provider_name}",
                verification=Verification(
                    status=VerificationStatus.FAILED,
                    source=source,
                    reason=(
                        f"provider rejected the operator key ({detail})"
                    ),
                ),
            )
        reason_by_kind = {
            "redirect": (
                "provider redirected the catalog request; redirects are never "
                "followed and credentials are never forwarded"
            ),
            "http_error": f"provider catalog returned {detail}",
            "network": f"provider catalog could not be read ({detail})",
            "malformed": f"provider catalog was not a usable document ({detail})",
            "oversize": f"provider catalog exceeded its bound ({detail})",
            "invalid_url": f"catalog endpoint is not usable ({detail})",
        }
        return ModelDescriptor(
            model_id=preset.descriptor_id,
            display_name=f"{preset.model_id} via {preset.provider_name}",
            verification=Verification(
                status=VerificationStatus.UNKNOWN,
                source=source,
                reason=reason_by_kind.get(kind, "catalog check inconclusive"),
            ),
        )

    async def discover_models(self, ctx: RuntimeContext) -> list[ModelDescriptor]:
        """Verify each preset's exact model id against its official catalog."""
        return [
            await self._catalog_descriptor(preset)
            for preset in self._presets.values()
        ]

    # -------------------------------------------------------------- execute

    def _event_kwargs(self, request: NormalizedRequest, sequence: int) -> dict[str, Any]:
        return {
            "run_id": request.run_id,
            "sequence": sequence,
            "timestamp": datetime.now(timezone.utc),
        }

    @staticmethod
    def _serialize_messages(request: NormalizedRequest) -> str:
        """Flatten roles into the one-shot query text; roles are serialized."""
        return "\n\n".join(
            f"{message.role}: {message.content}" for message in request.messages
        )

    def _config_for(self, preset: ProviderPreset) -> dict[str, Any]:
        """Strict task-local Hermes config: one provider, no fallback."""
        config: dict[str, Any] = {
            "model": {
                "default": preset.model_id,
                "provider": preset.provider_name,
                "reasoning_echo": True,
            },
            "agent": {
                "api_max_retries": 1,
                "reasoning_effort": self._reasoning,
            },
            "compression": {"enabled": False},
            "auxiliary": {
                "title_generation": {"enabled": False, "model_upgrade_enabled": False},
            },
            "memory": {
                "memory_enabled": False,
                "user_profile_enabled": False,
                "provider": "",
            },
            "skills": {
                "external_dirs": [],
                "project_discovery": False,
                "auto_load": [],
            },
            "curator": {"enabled": False},
            "plugins": {"enabled": []},
            "mcp_servers": {},
            "fallback_providers": [],
            "auth": {"adopt_external_logins": False},
            "security": {"tirith_enabled": False},
        }
        if preset.provider_kind == "builtin":
            # The bundled profile owns endpoint behaviour; pin provider + URL.
            config["model"]["base_url"] = preset.base_url
        else:
            config["providers"] = {
                preset.provider_name: {
                    "base_url": preset.base_url,
                    "api_mode": preset.api_mode,
                    "key_env": preset.key_env,
                    "model": preset.model_id,
                }
            }
        return config

    def _scrubbed_env_names(self, preset: ProviderPreset) -> list[str]:
        """Env names stripped from the child: everything secret-shaped or
        provider-owned except the *selected* preset's own key env."""
        return sorted(
            name
            for name in os.environ
            if name != preset.key_env
            and (
                _SECRET_NAME_RE.search(name)
                or name.startswith(_SCRUB_PREFIXES)
            )
        )

    def _argv(self, preset: ProviderPreset, home: Path, query_file: Path,
            workspace: str, run_budget: int) -> list[str]:
        # ``env -u`` unsets by name only — values never appear in argv. The
        # HERMES_HOME assignment lands after any ``-u HERMES_HOME``, so the
        # operator's own HERMES_HOME cannot leak through either. The child
        # inherits the executor's operator-bound environment minus every
        # unrelated provider/secret-shaped variable.
        argv = ["env"]
        for name in self._scrubbed_env_names(preset):
            argv += ["-u", name]
        return argv + [
            f"HERMES_HOME={home}",
            self._cli, "chat",
            "--query-file", str(query_file),
            "--oneshot",
            "--provider", preset.provider_name,
            "--model", preset.model_id,
            "--reasoning", self._reasoning,
            "--toolsets", "file,terminal",
            "--format", "stream-json",
            "--in", workspace,
            "--max-turns", str(self._max_turns),
            "--run-budget", str(run_budget),
            "--yolo",
            "--ignore-rules",
        ]

    def _redact(self, text: str) -> str:
        """Strip secret-shaped tokens and any resolvable key values."""
        redacted = _SECRET_RE.sub("[redacted]", text)
        env_names = {preset.key_env for preset in self._presets.values()}
        for name in env_names:
            value = os.environ.get(name)
            if value and len(value) >= 8:
                redacted = redacted.replace(value, "[redacted]")
        return redacted[:_MAX_ERROR_CHARS]

    async def execute(
        self, request: NormalizedRequest, ctx: RuntimeContext
    ) -> AsyncIterator[RunEvent]:
        sequence = 1

        def fail(code: str, message: str) -> RunEvent:
            return RunFailedEvent(
                **self._event_kwargs(request, sequence),
                payload=RunFailedPayload(code=code, message=message),
            )

        # Every rejection below happens before any process spawn (no effects).
        preset = self._presets.get(request.preset)
        if preset is None:
            yield fail(
                "unknown_preset",
                f"preset {request.preset!r} is not an operator-pinned backend",
            )
            return
        if request.model_alias is not None and request.model_alias not in (
            preset.model_id, preset.descriptor_id
        ):
            yield fail(
                "model_alias_mismatch",
                f"model_alias {request.model_alias!r} does not match the "
                f"preset pin {preset.model_id!r}",
            )
            return
        if self._reasoning not in REASONING_LEVELS:
            yield fail(
                "unsupported_reasoning",
                f"reasoning {self._reasoning!r} is not an approved native "
                f"value ({sorted(REASONING_LEVELS)})",
            )
            return
        if ctx.executor is None:
            yield fail(
                "no_process_executor",
                "the runner supplied no process executor; refusing to run",
            )
            return
        if not request.deadline_seconds or request.deadline_seconds <= 0:
            yield fail(
                "no_deadline",
                "a finite deadline is required to run the CLI",
            )
            return
        if ctx.permissions is None or not ctx.permissions.allows(YOLO_ACTION):
            yield fail(
                "yolo_not_preapproved",
                f"task policy does not preapprove {YOLO_ACTION!r}; "
                "refusing to spawn an unrestricted tool loop",
            )
            return

        # Catalog gate before any effect: the pinned model id must be verified
        # against the provider's official catalog on *this* run, not merely at
        # discovery time — a long-lived Registry cache must never turn a stale
        # listing into a permanent authorization. The TTL cache keeps this
        # bounded; a lost membership or unreachable catalog fails the run
        # before the task home or process is created.
        descriptor = await self._catalog_descriptor(preset)
        catalog = descriptor.verification
        if catalog.status is not VerificationStatus.PASSED:
            yield fail(
                "catalog_not_verified",
                self._redact(
                    f"provider catalog verification for {preset.alias} is "
                    f"{catalog.status.value}: {catalog.reason}; refusing to "
                    "run"
                ),
            )
            return

        # --- task-local HERMES_HOME + generated config + query file --------
        try:
            if self._state_dir:
                base = Path(self._state_dir)
                base.mkdir(parents=True, exist_ok=True)
                home = Path(tempfile.mkdtemp(
                    prefix=f"hermes-{request.run_id}-{request.attempt_id}-",
                    dir=base))
            else:
                home = Path(tempfile.mkdtemp(prefix="hermes-api-"))
            os.chmod(home, 0o700)
        except OSError as exc:
            yield fail("task_home_error", f"could not create task home: {exc}")
            return

        try:
            query_file = home / "query.txt"
            fd = os.open(query_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self._serialize_messages(request))
            config_path = home / "config.yaml"
            fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                # JSON is a YAML-1.2 subset: emitted, never string-interpolated.
                fh.write(json.dumps(self._config_for(preset), indent=2))
        except OSError as exc:
            yield fail("config_error", f"could not write task config: {exc}")
            return

        if ctx.workspace is not None:
            workspace = ctx.workspace.root
        else:
            workspace = str(home / "workspace")
        try:
            os.makedirs(workspace, mode=0o700, exist_ok=True)
        except OSError as exc:
            yield fail("workspace_error", f"workspace unusable: {exc}")
            return

        # The driver watchdog and Hermes' own budget share one bound; whichever
        # fires first ends the run (a bound trip is reported as cancelled, not
        # as a provider failure — see the deadline check inside the loop).
        run_budget = max(1, int(min(request.deadline_seconds, self._run_budget_cap)))
        deadline_at = time.monotonic() + request.deadline_seconds

        argv = self._argv(preset, home, query_file, workspace, run_budget)
        try:
            process = await ctx.executor.spawn(argv, cwd=workspace)
        except ProcessStartError as exc:
            yield fail("cli_not_startable", str(exc))
            return

        transport = NdjsonProcessTransport(
            process,
            max_frame_bytes=self._max_frame_bytes,
            grace_seconds=self._grace_seconds,
        )
        self._active[request.run_id] = transport
        cancel_event = asyncio.Event()
        self._cancel_events[request.run_id] = cancel_event
        saw_init = False
        saw_tool_error = False
        tool_seq = 0
        terminal_sent = False

        async def forced_stop(code: str, message: str) -> list[RunEvent]:
            """Terminate and classify our own stop; never relabel it a provider fault."""
            nonlocal sequence
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
                    payload=RunFailedPayload(
                        code=code, message=self._redact(message)),
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
                        "missing_result", "CLI stream ended without a result record"
                    ):
                        yield event
                        terminal_sent = True
                    return

                record_type = frame.get("type")
                if not isinstance(record_type, str) or record_type not in KNOWN_TYPES:
                    for event in await forced_stop(
                        "unknown_event",
                        "CLI emitted a record outside the stream-json protocol",
                    ):
                        yield event
                        terminal_sent = True
                    return

                if record_type == "system":
                    if frame.get("subtype") != "init":
                        for event in await forced_stop(
                            "unknown_event", "unrecognized system record"
                        ):
                            yield event
                            terminal_sent = True
                        return
                    saw_init = True
                    declared_model = frame.get("model")
                    if isinstance(declared_model, str) and declared_model and (
                        declared_model != preset.model_id
                    ):
                        for event in await forced_stop(
                            "model_mismatch",
                            f"CLI resolved model {declared_model!r}, expected "
                            f"{preset.model_id!r}",
                        ):
                            yield event
                            terminal_sent = True
                        return
                    continue

                if not saw_init:
                    for event in await forced_stop(
                        "protocol_error", "CLI emitted a record before init"
                    ):
                        yield event
                        terminal_sent = True
                    return

                if record_type == "text":
                    text = frame.get("text")
                    if isinstance(text, str) and text:
                        yield MessageDeltaEvent(
                            **self._event_kwargs(request, sequence),
                            payload=MessageDeltaPayload(text=text),
                        )
                        sequence += 1
                    continue

                if record_type == "tool_use":
                    tool_seq += 1
                    call_id = frame.get("tool_call_id") or f"{frame.get('name') or 'tool'}-{tool_seq}"
                    yield ToolStartedEvent(
                        **self._event_kwargs(request, sequence),
                        payload=ToolStartedPayload(
                            tool_call_id=str(call_id),
                            name=str(frame.get("name") or "tool"),
                        ),
                    )
                    sequence += 1
                    continue

                if record_type == "tool_result":
                    call_id = frame.get("tool_call_id") or f"{frame.get('name') or 'tool'}-{tool_seq}"
                    is_error = bool(frame.get("is_error"))
                    saw_tool_error = saw_tool_error or is_error
                    yield ToolCompletedEvent(
                        **self._event_kwargs(request, sequence),
                        payload=ToolCompletedPayload(
                            tool_call_id=str(call_id),
                            status="failed" if is_error else "completed",
                        ),
                    )
                    sequence += 1
                    continue

                # record_type == "result": the authoritative terminal record.
                if time.monotonic() >= deadline_at:
                    # The bound fired concurrently with the CLI's own stop; a
                    # bound trip is a cancellation, never a provider fault.
                    for event in await forced_stop("deadline", "run deadline expired"):
                        yield event
                        terminal_sent = True
                    return

                await transport.terminate()
                exit_code = frame.get("exit_code")
                error = frame.get("error")
                if (isinstance(exit_code, int) and exit_code != 0) or error:
                    detail = f"hermes reported failure (exit {exit_code!r})"
                    if isinstance(error, str) and error:
                        detail += f": {error}"
                    yield RunFailedEvent(
                        **self._event_kwargs(request, sequence),
                        payload=RunFailedPayload(
                            code="cli_reported_error",
                            message=self._redact(detail),
                        ),
                    )
                else:
                    tokens = frame.get("tokens") or {}
                    usage = self._usage_from_tokens(tokens)
                    note = (
                        f"preset={preset.alias}; model={preset.model_id}; "
                        f"reasoning={self._reasoning} (configured and applied "
                        "on the wire request; provider-side application "
                        "unobserved). Tool execution and any file changes are "
                        "not independently verified by this driver."
                    )
                    if saw_tool_error:
                        note += " One or more tool calls returned an error."
                    yield RunCompletedEvent(
                        **self._event_kwargs(request, sequence),
                        payload=RunCompletedPayload(
                            outcome="partial" if saw_tool_error else "succeeded",
                            usage=usage,
                            message=note,
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
                    "hermes-api run ended without a terminal event",
                    run_id=request.run_id,
                )

    @staticmethod
    def _usage_from_tokens(tokens: Any) -> Usage:
        """Hermes reports a token dict; all-zero means unreported, not free."""
        if isinstance(tokens, dict):
            input_tokens = tokens.get("input")
            output_tokens = tokens.get("output")
            if any(isinstance(v, (int, float)) and v > 0
                   for v in (input_tokens, output_tokens, tokens.get("total"))):
                return Usage(
                    provenance=UsageProvenance.REPORTED,
                    input_tokens=int(input_tokens or 0),
                    output_tokens=int(output_tokens or 0),
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
