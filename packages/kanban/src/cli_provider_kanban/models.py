"""Strict schemas for the Jev card contract, decision and routing policy.

The card contract (``TaskSpec``) is what the Hermes Lead embeds in a kanban
card body (fenced ``jev-task-spec`` JSON block or whole-body JSON) or maintains
in the local task map. ``JevDecision`` is the classifier's logical routing
output — never an execution, never a card mutation. Every model is frozen and
``extra="forbid"``: a card that tries to smuggle executor/workspace/model
overrides fails validation instead of being silently honoured.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class TaskSpec(_Schema):
    """The card contract every in-scope kanban card must carry."""

    task_id: str = Field(pattern=ID_PATTERN)
    task_revision: str = Field(min_length=1)
    role: Role
    capability: str = Field(min_length=1)
    tier: Tier
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


class JevDecision(_Schema):
    """Logical routing decision. ``route`` is ``role.capability.tier``."""

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
    # Ordered *available* logical candidates from the central route list;
    # a later compiler turns these into concrete 9Router combos. Empty when
    # nothing is available (hold) or the card could not be classified (replan).
    candidates: list[str] = Field(default_factory=list)
