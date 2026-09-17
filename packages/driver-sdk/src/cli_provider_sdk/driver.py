"""The exact ProviderDriver shape every driver distribution must satisfy.

Signatures take the ``RuntimeContext`` explicitly:

    probe(ctx) / discover_models(ctx) / execute(request, ctx) /
    cancel(run_id, ctx) / aclose()
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Protocol, runtime_checkable

from .types import (
    CancelResult,
    DriverManifest,
    ModelDescriptor,
    NormalizedRequest,
    ProbeReport,
    RunEvent,
    RuntimeContext,
)


@runtime_checkable
class ProviderDriver(Protocol):
    @property
    def manifest(self) -> DriverManifest: ...

    async def probe(self, ctx: RuntimeContext) -> ProbeReport: ...

    async def discover_models(self, ctx: RuntimeContext) -> list[ModelDescriptor]: ...

    def execute(
        self, request: NormalizedRequest, ctx: RuntimeContext
    ) -> AsyncIterator[RunEvent]: ...

    async def cancel(self, run_id: str, ctx: RuntimeContext) -> CancelResult: ...

    async def aclose(self) -> None: ...


class BaseDriver(ABC):
    """Convenience base implementing the trivial parts of ProviderDriver."""

    @property
    @abstractmethod
    def manifest(self) -> DriverManifest: ...

    @abstractmethod
    async def probe(self, ctx: RuntimeContext) -> ProbeReport: ...

    @abstractmethod
    async def discover_models(self, ctx: RuntimeContext) -> list[ModelDescriptor]: ...

    @abstractmethod
    def execute(
        self, request: NormalizedRequest, ctx: RuntimeContext
    ) -> AsyncIterator[RunEvent]: ...

    @abstractmethod
    async def cancel(self, run_id: str, ctx: RuntimeContext) -> CancelResult: ...

    async def aclose(self) -> None:
        return None
