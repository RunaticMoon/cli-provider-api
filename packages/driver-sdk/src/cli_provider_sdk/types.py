"""Exact public SDK type surface: ``cli_provider_sdk.types``.

These are the canonical names a driver author imports. They alias the fully
validated models in :mod:`cli_provider_sdk.models` and
:mod:`cli_provider_sdk.context`; there is no separate, unvalidated shape.
"""

from .context import RuntimeContext
from .models import (
    CancelResult,
    DriverManifest,
    ModelDescriptor,
    NormalizedRequest,
    ProbeReport,
    RunEvent,
)

__all__ = [
    "CancelResult",
    "DriverManifest",
    "ModelDescriptor",
    "NormalizedRequest",
    "ProbeReport",
    "RunEvent",
    "RuntimeContext",
]
