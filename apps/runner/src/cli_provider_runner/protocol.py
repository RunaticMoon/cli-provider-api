"""Internal Runner wire contract (versioned NDJSON RPC).

This contract is what the next API slice consumes so it never imports a driver.

Framing
-------
Newline-delimited JSON, one object per line, size-bounded. A frame that is not
a single valid JSON object is a protocol error; it is never repaired.

Envelopes
---------
Request   {"v":1,"type":"request","id":"<client-id>","method":"<name>","params":{...}}
Response  {"v":1,"type":"response","id":"<client-id>","ok":true,"result":{...}}
          {"v":1,"type":"response","id":"<client-id>","ok":false,
           "error":{"code":"...","message":"...","retryable":false}}
Event     {"v":1,"type":"event","request_id":"<client-id>","event":{...RunEvent...}}

A `run` call answers with zero or more `event` frames followed by exactly one
`response` frame. `ok:true` on that response means the RPC completed; the run's
own status/outcome live inside `result` and may still be `unknown`.

Methods
-------
manifest         -> DriverManifest
probe            -> ProbeReport
discover_models  -> {"models": [ModelDescriptor, ...]}
run              -> params RunParams; streamed events + RunResult
cancel           -> params CancelParams; CancelResult
shutdown         -> {"instance_id": ..., "stopping": true}

Events use the canonical wire names (run.started, message.delta, tool.started,
tool.completed, permission.required, artifact.created, usage.updated,
run.completed, run.failed, run.cancelled) with schema_version=1, run_id,
monotonic sequence and an aware timestamp. Structural events are forwarded with
their own kind and are never rewritten as answer deltas.

Security boundary
-----------------
A run request carries normalized references only (run/task/attempt/preset/
workspace and the message content). It can never select a package, module,
entry point, executable, cwd, env var or MCP command; extra fields are rejected.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cli_provider_sdk import (
    Alias,
    ID_PATTERN,
    NormalizedRequest,
    Message,
    RunEvent,
    WorkspaceRef,
)

PROTOCOL_VERSION = 1


class Method(str, Enum):
    MANIFEST = "manifest"
    PROBE = "probe"
    DISCOVER_MODELS = "discover_models"
    RUN = "run"
    CANCEL = "cancel"
    SHUTDOWN = "shutdown"


class ErrorCode(str, Enum):
    MALFORMED_REQUEST = "MALFORMED_REQUEST"
    FRAME_TOO_LARGE = "FRAME_TOO_LARGE"
    UNKNOWN_METHOD = "UNKNOWN_METHOD"
    INVALID_PARAMS = "INVALID_PARAMS"
    DRIVER_UNAVAILABLE = "DRIVER_UNAVAILABLE"
    QUEUE_FULL = "QUEUE_FULL"
    RUN_ALREADY_ACTIVE = "RUN_ALREADY_ACTIVE"
    RUN_NOT_FOUND = "RUN_NOT_FOUND"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class RunnerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: int = PROTOCOL_VERSION
    type: Literal["request"] = "request"
    id: str = Field(min_length=1)
    method: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False


class RunnerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: int = PROTOCOL_VERSION
    type: Literal["response"] = "response"
    id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: ErrorInfo | None = None


class RunnerEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: int = PROTOCOL_VERSION
    type: Literal["event"] = "event"
    request_id: str
    event: RunEvent


class RunParams(BaseModel):
    """Client-supplied run scope: normalized references plus message content."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=ID_PATTERN)
    task_id: str = Field(pattern=ID_PATTERN)
    attempt_id: str = Field(pattern=ID_PATTERN)
    preset: Alias
    workspace: WorkspaceRef
    model_alias: Alias | None = None
    messages: list[Message] = Field(min_length=1)
    deadline_seconds: float | None = Field(default=None, gt=0)

    def to_driver_request(self) -> NormalizedRequest:
        return NormalizedRequest(
            run_id=self.run_id,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            preset=self.preset,
            workspace=self.workspace,
            model_alias=self.model_alias,
            messages=self.messages,
            deadline_seconds=self.deadline_seconds,
        )


class CancelParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(pattern=ID_PATTERN)
