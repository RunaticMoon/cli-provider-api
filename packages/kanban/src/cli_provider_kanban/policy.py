"""Central routing policy — the single operator-owned routing authority.

Jev resolves the *logical* route ``role.capability.tier`` against this policy.
Candidate order is preserved verbatim; the 9Router compiler turns the ordered
candidates into concrete combos — neither the classifier nor the dispatcher
implements fallback itself. Codex is refused outright: it stays
Lead/final-fallback by policy and may not appear in any ordinary candidate
list.

Backend kinds carry an explicit ``transport``: ``native`` for Devin and
Antigravity (their official CLIs), ``api`` for B.AI and CommandCode (their
official model APIs per the task-2444 correction — API responses are model
responses, not completed agent runs).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cli_provider_sdk import ID_PATTERN

from .models import EffortHint, RiskFlag, Role, Tier

DEFAULT_ASSIGNEE = "jev-native"

# Mirrors kanban_db.VALID_STATUSES — duplicated deliberately so this package
# never imports the Hermes tree.
VALID_STATUSES = frozenset(
    {"triage", "todo", "scheduled", "ready", "running", "blocked", "review",
     "done", "archived"}
)

# Mirrors hermes_cli.profiles._PROFILE_ID_RE / _RESERVED_NAMES.
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PROFILE_RESERVED = frozenset({"hermes", "default", "test", "tmp", "root", "sudo"})

# Installed/known backend kinds. Codex is deliberately absent: it is the
# current Lead and Lead-decided FINAL fallback and never a combo member.
BackendKind = Literal["devin", "antigravity", "bai", "commandcode"]

# Per user correction (message 2444): B.AI and CommandCode connect through
# their official model APIs; Devin and Antigravity stay native CLI. The
# transport is declared explicitly in policy and validated against the kind.
KIND_TRANSPORT = {
    "devin": "native",
    "antigravity": "native",
    "bai": "api",
    "commandcode": "api",
}

# Verified wire values for the official B.AI/CommandCode APIs
# (ops/kanban-jev/ENVIRONMENT.md: low/high/max only). ``auto`` is never a wire
# value on these backends; it must be mapped explicitly.
API_EFFORT_VALUES = frozenset({"low", "high", "max"})

# Approval floors that no operator list may relax: hard/max tiers and every
# known risk flag always gate, and a backend whose cost is unknown can never
# be treated as free.
GATED_TIER_FLOOR = frozenset({Tier.HARD, Tier.MAX})
GATED_COST_FLOOR = frozenset({"unknown"})


class PolicyError(ValueError):
    """Invalid central policy or an unsupported mapping within it."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _assert_external_assignee(name: str) -> str:
    """The scope assignee must never resolve to a real Hermes profile.

    A collision would let the stock dispatcher spawn the card as
    ``hermes -p <name>`` — exactly what the shadow lane exists to prevent.
    """
    if not _PROFILE_ID_RE.match(name):
        raise ValueError(
            f"scope assignee {name!r} is not a valid external lane id "
            "(lowercase alphanumerics/-/_)"
        )
    if name in _PROFILE_RESERVED:
        raise ValueError(
            f"scope assignee {name!r} is a reserved Hermes profile name"
        )
    root = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    if (root / "profiles" / name).is_dir():
        raise ValueError(
            f"scope assignee {name!r} collides with an existing Hermes "
            f"profile under {root / 'profiles'} — pick a lane id no profile uses"
        )
    return name


class Scope(_Strict):
    assignee: str = DEFAULT_ASSIGNEE
    statuses: list[str] = Field(default_factory=lambda: ["ready", "todo"])

    @field_validator("assignee")
    @classmethod
    def _external(cls, value: str) -> str:
        return _assert_external_assignee(value)

    @field_validator("statuses")
    @classmethod
    def _real_statuses(cls, value: list[str]) -> list[str]:
        bad = [s for s in value if s not in VALID_STATUSES]
        if bad:
            raise ValueError(f"unknown kanban statuses {bad!r}")
        return value


class ConfidenceConfig(_Strict):
    """Advisory confidence values assigned by rule outcome. Never a gate."""

    body: float = Field(default=0.95, ge=0.0, le=1.0)
    task_map: float = Field(default=0.8, ge=0.0, le=1.0)
    no_spec: float = Field(default=0.5, ge=0.0, le=1.0)


class ClassifierConfig(_Strict):
    """Rules-first only; an LLM classifier is explicitly unsupported."""

    llm: Literal["disabled"] = "disabled"
    confidence: ConfidenceConfig = Field(default_factory=ConfidenceConfig)
    # Configurable floor: a would-be ``execute`` below this confidence is
    # replanned instead of dispatched. Risk/approval gates are unaffected —
    # they can never be relaxed by confidence.
    min_execute_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class Limits(_Strict):
    max_cards: int = Field(default=256, ge=1)
    max_body_bytes: int = Field(default=65_536, ge=1)
    max_spec_bytes: int = Field(default=32_768, ge=1)
    max_prompt_bytes: int = Field(default=32_768, ge=1)


class DecompositionLimits(_Strict):
    max_depth: int = Field(default=3, ge=0)
    max_children: int = Field(default=8, ge=0)
    replan_cap: int = Field(default=2, ge=0)


class NotifyTarget(_Strict):
    """Optional existing-Hermes notify subscription for approval handoff.

    These fields feed ``kanban_db_notify.add_notify_sub`` verbatim — the real
    gateway notifier owns delivery; there is no in-process callback pretending
    to be a cross-process RPC.
    """

    platform: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    thread_id: str | None = None
    user_id: str | None = None


class ApprovalPolicy(_Strict):
    tiers: list[Tier] = Field(default_factory=lambda: [Tier.HARD, Tier.MAX])
    risk_flags: list[RiskFlag] = Field(default_factory=lambda: list(RiskFlag))
    cost_tiers: list[Literal["free", "low", "standard", "high", "unknown"]] = (
        Field(default_factory=lambda: ["unknown"])
    )
    expiry_seconds: int = Field(default=86_400, ge=1)
    notify: NotifyTarget | None = None

    def gated_tiers(self) -> set[Tier]:
        """hard/max are floor gates: an operator list can add, never remove."""
        return set(self.tiers) | set(GATED_TIER_FLOOR)

    def gated_risk_flags(self) -> set[RiskFlag]:
        """Every known risk flag is a floor gate regardless of the list."""
        return set(self.risk_flags) | set(RiskFlag)

    def gated_cost_tiers(self) -> set[str]:
        """``unknown`` cost always gates — it is never free."""
        return set(self.cost_tiers) | set(GATED_COST_FLOOR)


class HermesConfig(_Strict):
    """How the dispatcher reaches the installed Hermes kernel."""

    python: str | None = None  # default: HERMES_PYTHON or <repo>/venv/bin/python
    repo: str | None = None    # default: HERMES_AGENT_DIR or ~/.hermes/hermes-agent


class DispatchConfig(_Strict):
    """Bounds for the thin dispatcher. No scheduling lives here."""

    claim_ttl_seconds: int = Field(default=900, ge=60)
    heartbeat_seconds: float = Field(default=60.0, gt=0)
    http_timeout_seconds: float = Field(default=120.0, gt=0)
    # One in-flight reservation per trusted workspace at a time.
    workspace_concurrency: int = Field(default=1, ge=1)
    max_cards_per_tick: int = Field(default=4, ge=1)
    # Agreed cross-worker contract: metadata.execution rides the chat request.
    # Kept switchable so the pre-merge baseline API (which rejects unknown
    # metadata keys) can still be exercised end-to-end.
    send_execution_metadata: bool = True


class ExecutionTarget(_Strict):
    """Where the dispatcher submits the single model request.

    ``direct`` posts to the wrapper's ``/v1/chat/completions`` with ``model``
    = a fixed operator-declared preset alias (the mock/dev lane). ``gateway``
    posts to a 9Router base URL with ``model`` = the compiled combo name —
    candidate ordering lives in the gateway, never here.
    """

    mode: Literal["direct", "gateway"] = "direct"
    base_url: str = Field(min_length=1)
    # Direct mode: wrapper preset alias. Gateway mode: combo name override
    # (default ``jev.<route>``); the combo's model order is compiled policy.
    model: str | None = None
    # Path to a file holding the bearer credential — read at call time, never
    # an argv value, never logged.
    credential_file: str | None = None

    @field_validator("base_url")
    @classmethod
    def _no_userinfo_or_query(cls, value: str) -> str:
        if not value.startswith("http://") and not value.startswith("https://"):
            raise ValueError("execution base_url must be http(s)")
        if any(ch in value for ch in ("@", "?", "#")):
            raise ValueError("execution base_url must not carry credentials/query")
        return value.rstrip("/")


class GatewayTarget(_Strict):
    name: str = Field(pattern=ID_PATTERN)
    url: str = Field(min_length=1)
    kind: Literal["disposable", "production"] = "disposable"


class GatewayConfig(_Strict):
    """Compilation target description for the 9Router policy compiler."""

    wrapper_base_url: str | None = None
    node_prefix: str = Field(default="jevwrap", pattern=ID_PATTERN)
    targets: list[GatewayTarget] = Field(default_factory=list)
    # Mutating combos stay non-operational until the shared Store/Controller
    # no-post-dispatch-retry guard is verified integrated (parallel core work).
    # This flag is the operator's explicit attestation, default off.
    assume_core_guard: bool = False


class ControlConfig(_Strict):
    """Configured operator identities allowed to run control operations.

    This is MVP local-operator auth: the CLI entrypoint plus a configured
    identity list. It is NOT Telegram auth and a ``--actor`` string alone
    never grants anything.
    """

    operators: list[str] = Field(default_factory=list)


class VerificationPolicy(_Strict):
    """Trusted verification executables and capture bounds."""

    # basename -> absolute executable path; a spec argv[0] not listed here is
    # rejected before the run. argv is always a list — never a shell string.
    executables: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_output_bytes: int = Field(default=65_536, ge=1)
    max_diff_bytes: int = Field(default=262_144, ge=1)

    @field_validator("executables")
    @classmethod
    def _absolute_paths(cls, value: dict[str, str]) -> dict[str, str]:
        for name, path in value.items():
            if not os.path.isabs(path):
                raise ValueError(
                    f"verification executable {name!r} must map to an absolute path"
                )
        return value


class WorkspaceEntry(_Strict):
    """Trusted workspace: referenced by id, never a card-chosen path.

    ``repo`` is the approved git repository; ``worktree_root`` is where
    per-card worktrees are created; ``wrapper_workspace_id`` is the
    pre-registered wrapper workspace the run binds to (the wrapper only
    accepts configured ids — a dynamic path is never selectable by request).
    """

    repo: str
    worktree_root: str
    wrapper_workspace_id: str | None = Field(default=None, pattern=ID_PATTERN)

    @field_validator("repo", "worktree_root")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not os.path.isabs(value):
            raise ValueError("workspace paths must be absolute")
        return value


class Backend(_Strict):
    """One logical candidate. ``capabilities`` maps capability -> verified.

    ``transport`` is explicit: ``native`` (devin/antigravity CLIs) or ``api``
    (bai/commandcode official model APIs). ``effort_map`` binds internal
    effort hints to verified per-backend wire values; a hint without an entry
    is a pre-execution error, never guessed.
    """

    id: str = Field(pattern=ID_PATTERN)
    kind: BackendKind
    transport: Literal["native", "api"]
    model: str | None = None
    # Wrapper preset alias / driver scope this backend executes through.
    preset: str | None = None
    driver: str | None = None
    enabled: bool = True
    # Declared-but-not-canaried: compiled for visibility, never applied.
    requires_canary: bool = False
    capabilities: dict[str, bool] = Field(default_factory=dict)
    cost_tier: Literal["free", "low", "standard", "high", "unknown"] = "unknown"
    effort_map: dict[str, str | None] = Field(default_factory=dict)
    disabled_reason: str | None = None

    @model_validator(mode="after")
    def _no_codex(self) -> "Backend":
        for field_value in (self.id, self.kind, self.model or ""):
            if "codex" in str(field_value).lower():
                raise ValueError(
                    "Codex may not appear in routing candidates: it is the "
                    "current Lead and Lead-decided FINAL fallback only"
                )
        return self

    @model_validator(mode="after")
    def _transport_matches_kind(self) -> "Backend":
        expected = KIND_TRANSPORT[self.kind]
        if self.transport != expected:
            raise ValueError(
                f"backend {self.id!r}: kind {self.kind!r} requires transport "
                f"{expected!r}, got {self.transport!r}"
            )
        return self

    @model_validator(mode="after")
    def _effort_map_values_are_verified(self) -> "Backend":
        for hint, wire in self.effort_map.items():
            if hint not in {h.value for h in EffortHint}:
                raise ValueError(
                    f"backend {self.id!r}: unknown effort hint {hint!r}"
                )
            if self.transport == "api":
                if wire not in API_EFFORT_VALUES:
                    raise ValueError(
                        f"backend {self.id!r}: api effort wire value must be "
                        f"one of {sorted(API_EFFORT_VALUES)}, got {wire!r}"
                    )
            elif self.kind == "devin":
                if wire is not None:
                    raise ValueError(
                        f"backend {self.id!r}: devin has no effort flag — the "
                        "only valid mapping is null (hint 'auto' maps null "
                        "implicitly)"
                    )
            elif wire is not None and not str(wire).strip():
                raise ValueError(
                    f"backend {self.id!r}: effort wire value must be a "
                    "non-empty string or null"
                )
        return self

    @model_validator(mode="after")
    def _api_backend_has_wiring(self) -> "Backend":
        # An api-transport backend executes through a compiled preset+driver;
        # without one the compiler could not route it — reject at load.
        if self.transport == "api" and not self.preset:
            raise ValueError(
                f"backend {self.id!r}: api transport requires a preset alias"
            )
        return self


class Route(_Strict):
    """Ordered central candidate list for one ``role.capability.tier``."""

    candidates: list[str] = Field(default_factory=list)


class Policy(_Strict):
    schema_version: Literal[1] = 1
    policy_version: str = Field(min_length=1)
    scope: Scope = Field(default_factory=Scope)
    task_map: str | None = None
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)
    limits: Limits = Field(default_factory=Limits)
    decomposition: DecompositionLimits = Field(default_factory=DecompositionLimits)
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    hermes: HermesConfig = Field(default_factory=HermesConfig)
    dispatch: DispatchConfig = Field(default_factory=DispatchConfig)
    execution: ExecutionTarget | None = None
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    verification: VerificationPolicy = Field(default_factory=VerificationPolicy)
    capabilities: list[str] = Field(min_length=1)
    backends: list[Backend] = Field(default_factory=list)
    routes: dict[str, Route] = Field(default_factory=dict)
    workspaces: dict[str, WorkspaceEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _cross_references(self) -> "Policy":
        ids = [b.id for b in self.backends]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate backend id")
        known = set(ids)
        capabilities = set(self.capabilities)
        for key, route in self.routes.items():
            parts = key.split(".")
            if len(parts) != 3:
                raise ValueError(
                    f"route {key!r} must be '<role>.<capability>.<tier>'"
                )
            role, capability, tier = parts
            if role not in {r.value for r in Role}:
                raise ValueError(f"route {key!r}: unknown role {role!r}")
            if capability not in capabilities:
                raise ValueError(
                    f"route {key!r}: capability {capability!r} is not declared "
                    "in policy.capabilities"
                )
            if tier not in {t.value for t in Tier}:
                raise ValueError(f"route {key!r}: unknown tier {tier!r}")
            unknown = [c for c in route.candidates if c not in known]
            if unknown:
                raise ValueError(
                    f"route {key!r} references unknown backends {unknown!r}"
                )
        target_names = [t.name for t in self.gateway.targets]
        if len(set(target_names)) != len(target_names):
            raise ValueError("duplicate gateway target name")
        return self

    def backend_map(self) -> dict[str, Backend]:
        return {b.id: b for b in self.backends}


def route_key(role: Role | str, capability: str, tier: Tier | str) -> str:
    r = role.value if isinstance(role, Role) else str(role)
    t = tier.value if isinstance(tier, Tier) else str(tier)
    return f"{r}.{capability}.{t}"


def available_candidates(policy: Policy, route: Route, capability: str) -> list[Backend]:
    """Enabled candidates with a true capability mapping, in policy order."""
    by_id = policy.backend_map()
    out = []
    for candidate_id in route.candidates:
        backend = by_id[candidate_id]
        if backend.enabled and backend.capabilities.get(capability) is True:
            out.append(backend)
    return out


class EffortUnsupported(PolicyError):
    """A card's effort hint has no verified mapping on a routed candidate."""


def resolve_effort(policy: Policy, route: Route, capability: str,
                   hint: EffortHint) -> dict[str, str | None]:
    """Map one effort hint to per-candidate wire values — or fail before claim.

    ``auto`` maps to ``None`` (no flag) implicitly on devin, which has no
    effort flag at all. Every other hint needs an explicit ``effort_map``
    entry on each available candidate; api backends only accept the verified
    wire values low/high/max (validated again at policy load).
    """
    by_id = policy.backend_map()
    resolved: dict[str, str | None] = {}
    unsupported: list[str] = []
    for candidate_id in route.candidates:
        backend = by_id[candidate_id]
        if not backend.enabled or backend.capabilities.get(capability) is not True:
            continue
        if hint is EffortHint.AUTO and backend.kind == "devin":
            resolved[backend.id] = None
            continue
        if hint.value in backend.effort_map:
            resolved[backend.id] = backend.effort_map[hint.value]
        else:
            unsupported.append(backend.id)
    if unsupported:
        raise EffortUnsupported(
            f"effort hint {hint.value!r} has no verified mapping on routed "
            f"candidate(s) {unsupported} — refusing before claim"
        )
    return resolved


def policy_fingerprint(policy: Policy) -> str:
    """Stable hash over the canonical policy document."""
    canonical = json.dumps(
        policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_policy(path: str | Path) -> Policy:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"policy file not found: {p}")
    raw = p.read_text(encoding="utf-8")
    try:
        data = json.loads(raw) if p.suffix == ".json" else yaml.safe_load(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"policy {p} is not parseable: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("policy root must be a mapping")
    return Policy.model_validate(data)
