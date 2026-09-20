"""Central routing policy — the single operator-owned routing authority.

Jev resolves the *logical* route ``role.capability.tier`` against this policy.
Candidate order is preserved verbatim; a later compiler turns the ordered
candidates into concrete 9Router combos — the classifier never implements
fallback itself. Codex is refused outright: it stays Lead/final-fallback by
policy and may not appear in any ordinary candidate list.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cli_provider_sdk import ID_PATTERN

from .models import RiskFlag, Role, Tier

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
BackendKind = Literal["devin", "bai_code", "commandcode", "antigravity"]


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


class Limits(_Strict):
    max_cards: int = Field(default=256, ge=1)
    max_body_bytes: int = Field(default=65_536, ge=1)
    max_spec_bytes: int = Field(default=32_768, ge=1)


class DecompositionLimits(_Strict):
    max_depth: int = Field(default=3, ge=0)
    max_children: int = Field(default=8, ge=0)
    replan_cap: int = Field(default=2, ge=0)


class ApprovalPolicy(_Strict):
    tiers: list[Tier] = Field(default_factory=lambda: [Tier.HARD, Tier.MAX])
    risk_flags: list[RiskFlag] = Field(default_factory=lambda: list(RiskFlag))


class WorkspaceEntry(_Strict):
    """Trusted workspace: referenced by id, never a card-chosen path."""

    path: str

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not os.path.isabs(value):
            raise ValueError("workspace path must be absolute")
        return value


class Backend(_Strict):
    """One logical candidate. ``capabilities`` maps capability -> verified."""

    id: str = Field(pattern=ID_PATTERN)
    kind: BackendKind
    model: str | None = None
    enabled: bool = True
    capabilities: dict[str, bool] = Field(default_factory=dict)
    cost_tier: Literal["free", "low", "standard", "high", "unknown"] = "unknown"
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
