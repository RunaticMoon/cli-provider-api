"""Runtime context services, expressed as protocols.

A RuntimeContext is dependency injection for a driver, NOT an OS sandbox. The
caller supplies whatever isolation it actually enforces; drivers must not infer
security guarantees from the presence of these objects.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProcessExecutor(Protocol):
    """Spawns the backing CLI. Drivers never choose the executable themselves."""

    async def spawn(self, argv: list[str], *, cwd: str | None = None) -> Any: ...


@runtime_checkable
class WorkspaceService(Protocol):
    @property
    def root(self) -> str: ...

    def resolve(self, relative: str) -> str: ...


@runtime_checkable
class PermissionPolicy(Protocol):
    def allows(self, action: str) -> bool: ...


@runtime_checkable
class Cancellation(Protocol):
    def is_requested(self) -> bool: ...

    async def wait(self) -> None: ...


@runtime_checkable
class RedactedLogger(Protocol):
    def info(self, message: str, **fields: Any) -> None: ...

    def warning(self, message: str, **fields: Any) -> None: ...


@runtime_checkable
class SessionStore(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def put(self, key: str, value: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class Deadline:
    expires_at: datetime | None = None

    def remaining(self) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - datetime.now(timezone.utc)).total_seconds()

    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0


class SimpleCancellation:
    """asyncio-native Cancellation implementation."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def request(self) -> None:
        self._event.set()

    def is_requested(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


class NullLogger:
    def info(self, message: str, **fields: Any) -> None:
        return None

    def warning(self, message: str, **fields: Any) -> None:
        return None


class InMemorySessionStore:
    def __init__(self) -> None:
        self._values: dict[str, dict[str, Any]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        return self._values.get(key)

    async def put(self, key: str, value: dict[str, Any]) -> None:
        self._values[key] = value


@dataclass
class RuntimeContext:
    executor: ProcessExecutor | None = None
    workspace: WorkspaceService | None = None
    permissions: PermissionPolicy | None = None
    cancellation: Cancellation = field(default_factory=SimpleCancellation)
    logger: RedactedLogger = field(default_factory=NullLogger)
    session_store: SessionStore = field(default_factory=InMemorySessionStore)
    deadline: Deadline = field(default_factory=Deadline)
