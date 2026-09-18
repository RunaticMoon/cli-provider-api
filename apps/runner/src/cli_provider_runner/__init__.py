"""cli_provider_runner — standalone allowlisted-driver runner.

The client and protocol submodules are the only surface the API/core may
import. The driver-loading (:mod:`cli_provider_runner.registry`) and server
(:mod:`cli_provider_runner.server`) modules are resolved lazily on first
attribute access, so importing the package for its protocol does not drag a
driver loader or the Runner server into the API process.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from .client import RunnerClient
from .protocol import (
    PROTOCOL_VERSION,
    CancelParams,
    ErrorCode,
    ErrorInfo,
    Method,
    RunParams,
    RunnerEventEnvelope,
    RunnerRequest,
    RunnerResponse,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imports at runtime
    from .registry import (
        DriverAllowlistEntry,
        DriverLoadError,
        load_driver,
        validate_manifest,
    )
    from .server import DEFAULT_MAX_RUN_SECONDS, ActiveRun, RunnerServer

__all__ = [
    "DEFAULT_MAX_RUN_SECONDS",
    "PROTOCOL_VERSION",
    "ActiveRun",
    "CancelParams",
    "DriverAllowlistEntry",
    "DriverLoadError",
    "ErrorCode",
    "ErrorInfo",
    "Method",
    "RunParams",
    "RunnerClient",
    "RunnerEventEnvelope",
    "RunnerRequest",
    "RunnerResponse",
    "RunnerServer",
    "load_driver",
    "validate_manifest",
]

_LAZY_ATTRIBUTES = {
    "DEFAULT_MAX_RUN_SECONDS": ".server",
    "ActiveRun": ".server",
    "RunnerServer": ".server",
    "DriverAllowlistEntry": ".registry",
    "DriverLoadError": ".registry",
    "load_driver": ".registry",
    "validate_manifest": ".registry",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_ATTRIBUTES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name, __name__), name)
    globals()[name] = value
    return value
