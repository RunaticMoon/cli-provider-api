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
    cached: bool
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


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
    synthetic: bool = True
    summary: str | None = None
    verification: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    detail: str | None = None
    created_at: str
    finished_at: str | None = None
