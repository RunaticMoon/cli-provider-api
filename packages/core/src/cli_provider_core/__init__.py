"""cli_provider_core — operator config, SQLite store, Runner registry, controller.

This package never imports or loads a driver package. It reaches Runners only
through the validated UDS client session.
"""

from .config import (
    ApiSettings,
    Concurrency,
    Limits,
    OperatorConfig,
    PresetConfig,
    PrincipalConfig,
    RunnerConfig,
    WorkspaceConfig,
    load_config,
)
from .controller import ActiveRun, CancelView, RunController, Submission
from .errors import (
    AuthenticationError,
    AuthorizationError,
    BodyTimeout,
    BodyTooLarge,
    Conflict,
    CoreError,
    HeadersTooLarge,
    InvalidRequest,
    NotFound,
    QueueFull,
    QueueTimeout,
    RunnerQuarantined,
    RunnerRunRejected,
    RunnerUnavailable,
    UnsupportedCapability,
    UpstreamProtocolError,
)
from .hashing import hash_api_key, request_hash, verify_api_key
from .ids import chat_id_for_run, new_artifact_id, new_attempt_id, new_run_id
from .models import ArtifactRecord, AttemptRecord, EventRecord, RunResultView
from .registry import PresetHealth, RunnerHealth, RunnerRegistry
from .runner import RunnerSession, UdsRunnerSession
from .store import Store

__all__ = [
    "ActiveRun",
    "ApiSettings",
    "ArtifactRecord",
    "AttemptRecord",
    "AuthenticationError",
    "AuthorizationError",
    "BodyTimeout",
    "BodyTooLarge",
    "CancelView",
    "Concurrency",
    "chat_id_for_run",
    "Conflict",
    "CoreError",
    "EventRecord",
    "HeadersTooLarge",
    "InvalidRequest",
    "Limits",
    "NotFound",
    "OperatorConfig",
    "PresetConfig",
    "PresetHealth",
    "PrincipalConfig",
    "QueueFull",
    "QueueTimeout",
    "RunController",
    "RunResultView",
    "RunnerConfig",
    "RunnerHealth",
    "RunnerQuarantined",
    "RunnerRegistry",
    "RunnerRunRejected",
    "RunnerSession",
    "RunnerUnavailable",
    "Store",
    "Submission",
    "UdsRunnerSession",
    "UnsupportedCapability",
    "UpstreamProtocolError",
    "WorkspaceConfig",
    "hash_api_key",
    "load_config",
    "new_artifact_id",
    "new_attempt_id",
    "new_run_id",
    "request_hash",
    "verify_api_key",
]
