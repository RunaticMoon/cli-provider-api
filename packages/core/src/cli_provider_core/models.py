"""Core run/event/artifact records and the normalized result view."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

RESERVED = "reserved"
QUEUED = "queued"
STARTING = "starting"
RUNNING = "running"
CANCELLING = "cancelling"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
UNKNOWN = "unknown"

TERMINAL_STATUSES = frozenset({COMPLETED, FAILED, CANCELLED, UNKNOWN})
ACTIVE_STATUSES = frozenset({RESERVED, QUEUED, STARTING, RUNNING, CANCELLING})
# Statuses that hold the per-caller logical lock for a task.
LOCK_STATUSES = ACTIVE_STATUSES | {UNKNOWN}

OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_PARTIAL = "partial"
OUTCOME_PROVIDER_ERROR = "provider_error"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_QUEUE_TIMEOUT = "queue_timeout"
OUTCOME_REJECTED = "rejected"

ENFORCED_TERMINAL = frozenset(
    {COMPLETED, FAILED, CANCELLED, UNKNOWN, OUTCOME_QUEUE_TIMEOUT, OUTCOME_REJECTED}
)


@dataclass(frozen=True)
class AttemptRecord:
    run_id: str
    principal: str
    task_id: str
    attempt_id: str
    preset: str
    driver_id: str
    runner_instance: str
    workspace_id: str
    request_hash: str
    status: str
    outcome: str | None
    summary: str | None
    detail: str | None
    verification: dict[str, Any] | None
    usage: dict[str, Any] | None
    # Derived from the verified Runner manifest at reservation, never assumed.
    synthetic: bool
    cached: bool
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    # Caller-supplied dispatcher execution context (task_revision /
    # base_revision / route / policy_version), persisted verbatim. It is
    # authenticated only in the sense that the request's principal key was
    # authenticated — the values are NOT server-attested route/policy proof,
    # are never model-supplied, and never select execution authority.
    execution: dict[str, Any] | None = None
    # Server-side model binding evidence: the admitted alias, the requested
    # native model id, the resolved exact id and any reasoning-effort request,
    # plus whether the alias came from a catalog source or a static preset.
    # Persisted verbatim at reservation; never model-supplied.
    model_binding: dict[str, Any] | None = None


@dataclass(frozen=True)
class EventRecord:
    run_id: str
    sequence: int
    kind: str
    event: dict[str, Any]
    timestamp: str


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    run_id: str
    principal: str
    preset: str
    workspace_id: str
    kind: str
    content_type: str
    size: int
    sha256: str
    path: str
    created_at: str


class RunResultView(BaseModel):
    """Normalized result extension returned alongside the OpenAI response."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_id: str
    attempt_id: str
    preset: str
    driver_id: str
    runner_instance: str
    workspace_id: str
    status: str
    outcome: str | None = None
    cached: bool = False
    # Truthful provenance: supplied from the persisted attempt, never defaulted.
    synthetic: bool
    # The exact caller-supplied execution context the attempt was reserved
    # with, echoed verbatim. It is not a server attestation of route/policy —
    # a trusted dispatcher must match it against its own durable receipt and
    # the canonical run/preset/model, never admit policy from the echo.
    execution: dict[str, Any] | None = None
    # Model binding evidence: requested/resolved native model ids, requested
    # effort, descriptor effort mode and whether the alias was dynamic.
    model: dict[str, Any] | None = None
    summary: str | None = None
    verification: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    detail: str | None = None
    created_at: str
    finished_at: str | None = None
