"""Strict schemas for the Jev card contract, decision and routing policy.

The card contract (``TaskSpec``) is what the Hermes Lead embeds in a kanban
card body (fenced ``jev-task-spec`` JSON block or whole-body JSON) or maintains
in the local task map. ``JevDecision`` is the classifier's logical routing
output — never an execution, never a card mutation, and never a backend
selection: the decision carries the logical ``route`` only. Every model is
frozen and ``extra="forbid"``: a card that tries to smuggle
executor/workspace/model overrides fails validation instead of being silently
honoured.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cli_provider_sdk import ID_PATTERN


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Role(str, Enum):
    WORKER = "worker"
    PLANNER = "planner"
    REVIEWER = "reviewer"
    RESEARCHER = "researcher"


class Tier(str, Enum):
    """Routing tiers — NOT provider effort levels."""

    FREE = "free"
    EASY = "easy"
    STANDARD = "standard"
    HARD = "hard"
    MAX = "max"


class EffortHint(str, Enum):
    """Internal effort hints; never mapped to a provider effort flag here."""

    AUTO = "auto"
    ECONOMY = "economy"
    BALANCED = "balanced"
    THOROUGH = "thorough"
    MAXIMUM = "maximum"


class WorkKind(str, Enum):
    """Structured work declaration kind — the deterministic role/capability seed."""

    IMPLEMENT = "implement"
    REVIEW = "review"
    RESEARCH = "research"
    PLAN = "plan"


class DesignReadiness(str, Enum):
    """Whether the card's design is settled enough to execute."""

    READY = "ready"
    DRAFT = "draft"
    UNCLEAR = "unclear"


class ScopeSize(str, Enum):
    """Coarse declared change scope feeding the tier derivation."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class RiskFlag(str, Enum):
    """Risk classes that always require user approval; confidence cannot override."""

    AUTHN = "authn"
    AUTHZ = "authz"
    SECURITY = "security"
    BILLING = "billing"
    DESTRUCTION = "destruction"
    MIGRATION = "migration"
    PRODUCTION = "production"
    EXTERNAL_EFFECTS = "external_effects"


class RecommendedAction(str, Enum):
    EXECUTE = "execute"
    REPLAN = "replan"
    NEEDS_APPROVAL = "needs_approval"
    # The required route exists but no candidate is enabled+mapped right now.
    HOLD = "hold"


def _revision_str(value: object) -> str:
    if isinstance(value, bool):
        raise ValueError("revision must be a string or integer")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("revision must be a non-empty string or integer")
    return value.strip()


class VerificationSpec(_Schema):
    """Trusted verification: an argv list (never a shell string) plus criteria."""

    argv: list[str] = Field(min_length=1)
    criteria: str = Field(min_length=1)


class DecompositionSpec(_Schema):
    """Planner-card fan-out declaration, bounded by policy limits."""

    depth: int = Field(default=0, ge=0)
    children: int = Field(default=0, ge=0)


class WorkSpec(_Schema):
    """Structured work declaration — the deterministic classifier input.

    ``kind`` derives role+capability, ``scope`` derives the tier, and
    ``design`` gates execution readiness. A card may ALSO carry the legacy
    ``role``/``capability``/``tier`` hints; when both are present the hints are
    Lead intent and must agree with the derivation — a conflict means the card
    is ambiguous and is sent to ``replan`` rather than guessed.
    """

    kind: WorkKind
    design: DesignReadiness = DesignReadiness.READY
    scope: ScopeSize = ScopeSize.MEDIUM


class TaskSpec(_Schema):
    """The card contract every in-scope kanban card must carry.

    ``role``/``capability``/``tier`` are optional Lead-intent hints, not the
    sole classifier input: a structured ``work`` block derives them when
    present. At least one of the two must be supplied; with no ``work`` block
    all three hints are required together.
    """

    task_id: str = Field(pattern=ID_PATTERN)
    task_revision: str = Field(min_length=1)
    # Optional Lead-intent hints — validated against the derived route when a
    # structured work block is present; required together when it is not.
    role: Role | None = None
    capability: str | None = Field(default=None, min_length=1)
    tier: Tier | None = None
    work: WorkSpec | None = None
    effort_hint: EffortHint = EffortHint.AUTO
    objective: str = Field(min_length=1)
    inputs: list[str]
    dependency_ids: list[str]
    relevant_files: list[str]
    allowed_scope: list[str] = Field(min_length=1)
    artifacts: list[str]
    verification: VerificationSpec
    acceptance_criteria: list[str] = Field(min_length=1)
    prohibited: list[str]
    base_revision: str = Field(min_length=1)
    workspace_id: str = Field(pattern=ID_PATTERN)
    risk_flags: list[RiskFlag]
    replan_count: int = Field(default=0, ge=0)
    decomposition: DecompositionSpec | None = None

    @field_validator("task_revision", mode="before")
    @classmethod
    def _coerce_revision(cls, value: object) -> str:
        return _revision_str(value)

    @model_validator(mode="after")
    def _route_inputs_present(self) -> "TaskSpec":
        if self.work is None:
            missing = [
                name
                for name, value in (
                    ("role", self.role),
                    ("capability", self.capability),
                    ("tier", self.tier),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    "spec carries no structured 'work' block, so the Lead "
                    f"intent hints are required together; missing: {missing}"
                )
        return self


class JevDecision(_Schema):
    """Logical routing decision. ``route`` is ``role.capability.tier``.

    The decision deliberately carries NO backend candidate list: candidate
    ordering belongs to the central policy and the 9Router compiler, not to
    Jev. ``recommended_action`` plus ``route`` is the whole answer.
    """

    task_id: str = Field(pattern=ID_PATTERN)
    task_revision: str = Field(min_length=1)
    role: Role
    capability: str | None = None
    tier: Tier | None = None
    effort_hint: EffortHint | None = None
    route: str | None = None
    recommended_action: RecommendedAction
    risk_flags: list[RiskFlag] = Field(default_factory=list)
    # Advisory only — a rules-derived hint, never a probability and never a
    # gate for risk/approval behaviour.
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    policy_version: str = Field(min_length=1)
