"""Typed schemas for the ProviderDriver SDK.

Everything the API/Runner boundary and the drivers exchange is validated here.
Completion status, outcome and verification are deliberately separate concepts:
a terminal ``run.completed`` never implies the task was fulfilled (it may be
``partial``) and never implies verified work, and unknown usage is ``None``
rather than an invented zero.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, TypeAlias, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

SDK_VERSION = "1.0"

# Strict identifiers for run/task/attempt/workspace and driver ids.
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

# Preset/model aliases are NOT filesystem references and are NOT normalized.
# They are dotted/slashed public names such as ``mock/text`` or ``agy/review``.
ALIAS_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
MAX_ALIAS_SEGMENTS = 4
MAX_ALIAS_LENGTH = 259


def validate_alias(value: str) -> str:
    if not value:
        raise ValueError("alias must not be empty")
    if len(value) > MAX_ALIAS_LENGTH:
        raise ValueError(f"alias exceeds {MAX_ALIAS_LENGTH} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("alias must not contain control characters")
    segments = value.split("/")
    if len(segments) > MAX_ALIAS_SEGMENTS:
        raise ValueError(f"alias must have at most {MAX_ALIAS_SEGMENTS} segments")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise ValueError("alias must not contain empty or traversal segments")
        if ALIAS_SEGMENT.match(segment) is None:
            raise ValueError(f"alias segment {segment!r} is not a valid name")
    return value


Alias = Annotated[str, AfterValidator(validate_alias)]

# Dispatcher route in `role.capability.tier` form (e.g. ``worker.code.standard``):
# exactly three bounded dot-separated segments, never a path or URL.
ROUTE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}(\.[A-Za-z0-9][A-Za-z0-9_-]{0,62}){2}$"


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TransportKind(str, Enum):
    STDIO = "stdio"
    ACP = "acp"
    PTY = "pty"


class StreamingMode(str, Enum):
    NATIVE = "native"
    BUFFERED = "buffered"
    NONE = "none"


class SessionMode(str, Enum):
    NONE = "none"
    EXPLICIT_RESUME = "explicit_resume"
    PERSISTENT = "persistent"


class RoleMode(str, Enum):
    NATIVE = "native"
    SERIALIZED = "serialized"
    UNSUPPORTED = "unsupported"


class StructuredOutputMode(str, Enum):
    NATIVE = "native"
    VALIDATED = "validated"
    NONE = "none"


class UsageProvenance(str, Enum):
    REPORTED = "reported"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    UNKNOWN = "unknown"


class CompletionStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class Outcome(str, Enum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    PROVIDER_ERROR = "provider_error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class EventKind(str, Enum):
    RUN_STARTED = "run.started"
    MESSAGE_DELTA = "message.delta"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    PERMISSION_REQUIRED = "permission.required"
    ARTIFACT_CREATED = "artifact.created"
    USAGE_UPDATED = "usage.updated"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"


TERMINAL_KINDS = frozenset(
    {EventKind.RUN_COMPLETED, EventKind.RUN_FAILED, EventKind.RUN_CANCELLED}
)

# Event kinds that carry model-visible answer text. Everything else (tool logs,
# permissions, artifacts) must never be forwarded as an answer delta.
ANSWER_KINDS = frozenset({EventKind.MESSAGE_DELTA})


def is_terminal_kind(kind: EventKind | str) -> bool:
    return kind in TERMINAL_KINDS


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


class DriverManifest(_Schema):
    driver_id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sdk_version: str = Field(min_length=1)
    protocol_family: str = Field(min_length=1)
    supported_transports: list[TransportKind] = Field(min_length=1)
    synthetic: bool = False


class Capabilities(_Schema):
    """Declared capability matrix. Unimplemented features are explicit and false."""

    streaming: StreamingMode
    sessions: SessionMode
    roles: RoleMode
    structured_output: StructuredOutputMode
    external_tool_calls: bool
    internal_tools: bool
    vision: bool
    workspace_write: bool
    web_search: bool
    usage: UsageProvenance


class Verification(_Schema):
    status: VerificationStatus
    source: str = Field(min_length=1)
    reason: str | None = None


class ProbeReport(_Schema):
    ok: bool
    driver_id: str = Field(pattern=ID_PATTERN)
    driver_version: str = Field(min_length=1)
    cli_version: str | None = None
    capabilities: Capabilities
    notes: list[str] = Field(default_factory=list)


class ModelDescriptor(_Schema):
    model_id: str = Field(pattern=ID_PATTERN)
    display_name: str = Field(min_length=1)
    verification: Verification


class WorkspaceRef(_Schema):
    """A normalized workspace reference. Never a filesystem path or cwd."""

    workspace_id: str = Field(pattern=ID_PATTERN)
    revision: str | None = Field(default=None, pattern=ID_PATTERN)


class Message(_Schema):
    role: Literal["system", "user", "assistant"]
    content: str


class ExecutionContext(_Schema):
    """Authenticated dispatcher-supplied execution identity for one attempt.

    Complete-if-present: all four bounded scalar fields are required together.
    The values are opaque to the runtime — they are evidence metadata propagated
    to the worker and back, never an executable/path/right selector and never
    model-supplied text. Wrapper-generated ``run_id``/``attempt_id`` remain
    canonical and are never part of this context.
    """

    task_revision: str = Field(pattern=ID_PATTERN)
    base_revision: str = Field(pattern=ID_PATTERN)
    route: str = Field(pattern=ROUTE_PATTERN)
    policy_version: str = Field(pattern=ID_PATTERN)


class NormalizedRequest(_Schema):
    run_id: str = Field(pattern=ID_PATTERN)
    task_id: str = Field(pattern=ID_PATTERN)
    attempt_id: str = Field(pattern=ID_PATTERN)
    preset: Alias
    workspace: WorkspaceRef
    model_alias: Alias | None = None
    messages: list[Message] = Field(min_length=1)
    deadline_seconds: float | None = Field(default=None, gt=0)
    # Optional authenticated execution context (backwards-compatible: absent
    # means a legacy request with no dispatcher metadata).
    execution: ExecutionContext | None = None


class Usage(_Schema):
    provenance: UsageProvenance
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _unknown_has_no_counts(self) -> "Usage":
        if self.provenance is UsageProvenance.UNKNOWN:
            if self.input_tokens is not None or self.output_tokens is not None:
                raise ValueError("unknown usage must not carry token counts")
        return self


class CancelResult(_Schema):
    run_id: str = Field(pattern=ID_PATTERN)
    requested: bool
    requested_at: datetime
    confirmed: bool
    confirmed_at: datetime | None = None
    deadline_seconds: float = Field(gt=0)
    detail: str | None = None

    @field_validator("requested_at", "confirmed_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        return value if value is None else _ensure_aware(value)


class RunResult(_Schema):
    """RPC-level run outcome. ``status``, ``outcome`` and ``verification`` differ.

    ``completed`` does not imply the task succeeded: a completed run may carry
    ``outcome=partial``. There is no one-to-one status/outcome mapping.
    """

    run_id: str = Field(pattern=ID_PATTERN)
    status: CompletionStatus
    outcome: Outcome
    verification: Verification
    usage: Usage
    terminal_kind: EventKind | None = None
    terminal_sequence: int | None = Field(default=None, ge=1)
    events_seen: int = Field(default=0, ge=0)
    synthetic: bool = False
    detail: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "RunResult":
        if self.terminal_kind is not None and not is_terminal_kind(self.terminal_kind):
            raise ValueError("terminal_kind must be a terminal event kind")

        allowed: dict[CompletionStatus, frozenset[Outcome]] = {
            CompletionStatus.COMPLETED: frozenset(
                {Outcome.SUCCEEDED, Outcome.PARTIAL}
            ),
            CompletionStatus.FAILED: frozenset({Outcome.PROVIDER_ERROR}),
            CompletionStatus.CANCELLED: frozenset({Outcome.CANCELLED}),
            CompletionStatus.UNKNOWN: frozenset({Outcome.UNKNOWN}),
        }
        if self.outcome not in allowed[self.status]:
            raise ValueError(
                f"outcome {self.outcome.value!r} is not valid for status "
                f"{self.status.value!r}"
            )

        if self.status is CompletionStatus.UNKNOWN:
            if self.terminal_kind is not None:
                raise ValueError("unknown status must not claim a terminal event")
        elif self.status is CompletionStatus.COMPLETED:
            if self.terminal_kind is not EventKind.RUN_COMPLETED:
                raise ValueError("completed status requires a run.completed terminal")
        elif self.status is CompletionStatus.FAILED:
            if self.terminal_kind is not EventKind.RUN_FAILED:
                raise ValueError("failed status requires a run.failed terminal")
        elif self.status is CompletionStatus.CANCELLED:
            if self.terminal_kind not in (None, EventKind.RUN_CANCELLED):
                raise ValueError("cancelled status requires a run.cancelled terminal")
        return self


class _EventBase(_Schema):
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=ID_PATTERN)
    sequence: int = Field(ge=1)
    timestamp: datetime
    synthetic: bool = False

    @field_validator("timestamp")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _ensure_aware(value)


class RunStartedPayload(_Schema):
    preset: Alias
    model_alias: Alias | None = None


class MessageDeltaPayload(_Schema):
    text: str = Field(min_length=1)


class ToolStartedPayload(_Schema):
    tool_call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class ToolCompletedPayload(_Schema):
    tool_call_id: str = Field(min_length=1)
    status: Literal["completed", "failed"]
    detail: str | None = None


class PermissionRequiredPayload(_Schema):
    request_id: str = Field(min_length=1)
    action: str = Field(min_length=1)


class ArtifactCreatedPayload(_Schema):
    artifact_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)


class UsageUpdatedPayload(_Schema):
    usage: Usage


class RunCompletedPayload(_Schema):
    outcome: Literal["succeeded", "partial"] = "succeeded"
    usage: Usage
    message: str | None = None


class RunFailedPayload(_Schema):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class RunCancelledPayload(_Schema):
    reason: str = Field(min_length=1)


class RunStartedEvent(_EventBase):
    kind: Literal["run.started"] = "run.started"
    payload: RunStartedPayload


class MessageDeltaEvent(_EventBase):
    kind: Literal["message.delta"] = "message.delta"
    payload: MessageDeltaPayload


class ToolStartedEvent(_EventBase):
    kind: Literal["tool.started"] = "tool.started"
    payload: ToolStartedPayload


class ToolCompletedEvent(_EventBase):
    kind: Literal["tool.completed"] = "tool.completed"
    payload: ToolCompletedPayload


class PermissionRequiredEvent(_EventBase):
    kind: Literal["permission.required"] = "permission.required"
    payload: PermissionRequiredPayload


class ArtifactCreatedEvent(_EventBase):
    kind: Literal["artifact.created"] = "artifact.created"
    payload: ArtifactCreatedPayload


class UsageUpdatedEvent(_EventBase):
    kind: Literal["usage.updated"] = "usage.updated"
    payload: UsageUpdatedPayload


class RunCompletedEvent(_EventBase):
    kind: Literal["run.completed"] = "run.completed"
    payload: RunCompletedPayload


class RunFailedEvent(_EventBase):
    kind: Literal["run.failed"] = "run.failed"
    payload: RunFailedPayload


class RunCancelledEvent(_EventBase):
    kind: Literal["run.cancelled"] = "run.cancelled"
    payload: RunCancelledPayload


RunEvent: TypeAlias = Annotated[
    Union[
        RunStartedEvent,
        MessageDeltaEvent,
        ToolStartedEvent,
        ToolCompletedEvent,
        PermissionRequiredEvent,
        ArtifactCreatedEvent,
        UsageUpdatedEvent,
        RunCompletedEvent,
        RunFailedEvent,
        RunCancelledEvent,
    ],
    Field(discriminator="kind"),
]

RUN_EVENT_ADAPTER: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)


def terminal_kind_for(status: CompletionStatus) -> EventKind | None:
    return {
        CompletionStatus.COMPLETED: EventKind.RUN_COMPLETED,
        CompletionStatus.FAILED: EventKind.RUN_FAILED,
        CompletionStatus.CANCELLED: EventKind.RUN_CANCELLED,
    }.get(status)
