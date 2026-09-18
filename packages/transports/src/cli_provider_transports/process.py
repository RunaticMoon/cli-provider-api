"""Bounded stdio subprocess transport for CLI drivers.

Design rules enforced here:

* the backing CLI is spawned from an explicit ``argv`` list, never a shell, and
  the executable/args come from operator configuration - never from a request;
* the child gets its own process group so a group ``SIGTERM`` reaches its
  descendants, and ``SIGKILL`` is escalated while the leader is still alive. A
  descendant that ignores ``SIGTERM`` and outlives an already-reaped leader is
  deliberately not chased (its pgid may be reusable) and is only *reported*;
* protocol frames live on stdout, diagnostics on stderr, and stderr text can
  never be merged into the event stream or become answer content;
* frames, total output and stderr are all bounded;
* termination is *confirmed*: ``terminate()`` reports True only once the process
  is actually gone.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any, Mapping, Protocol, runtime_checkable

from .ndjson import DEFAULT_MAX_FRAME_BYTES, FrameReader, TransportError

DEFAULT_MAX_STDERR_BYTES = 8192
DEFAULT_TERMINATION_GRACE_SECONDS = 3.0


class ProcessStartError(TransportError):
    """The backing CLI could not be started at all (pre-execution failure)."""


@runtime_checkable
class SpawnedProcess(Protocol):
    """Minimal handle contract a process executor must return."""

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    @property
    def stdin(self) -> Any: ...

    @property
    def stdout(self) -> Any: ...

    @property
    def stderr(self) -> Any: ...

    async def wait(self) -> int: ...


class LocalProcessExecutor:
    """Default executor: explicit argv, own process group, inherited environment.

    This implements the SDK ``ProcessExecutor`` protocol. It performs no
    sandboxing of its own; the caller decides whether the argv is allowed.
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = dict(env) if env is not None else None

    async def spawn(self, argv: list[str], *, cwd: str | None = None) -> SpawnedProcess:
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise ProcessStartError("argv must be a non-empty list of non-empty strings")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=self._env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise ProcessStartError(
                f"could not start {argv[0]!r}: {type(exc).__name__}"
            ) from exc
        return process


def group_members(pgid: int) -> list[int]:
    """Best-effort list of live processes in ``pgid`` (Linux ``/proc`` scan).

    Used to *report* survivors, never to signal them by group id: once the group
    leader has been reaped its pid (and therefore the pgid) can be reused, so a
    post-reap group kill is unsafe. Returns an empty list when ``/proc`` is
    unavailable or unreadable.
    """
    if pgid <= 0 or not os.path.isdir("/proc"):
        return []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    members: list[int] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                fields = handle.read().rsplit(b")", 1)[-1].split()
        except OSError:
            continue
        # After the comm field: state ppid pgrp session ...
        # A zombie still shows its old pgrp, so exclude it: "still alive" must
        # mean alive, not "awaiting reaping".
        if (
            len(fields) >= 3
            and fields[0] != b"Z"
            and fields[2].isdigit()
            and int(fields[2]) == pgid
        ):
            members.append(pid)
    return members


def leader_alive(pid: int) -> bool:
    """Is the group leader still running (not a zombie, not gone)?

    Guards **every** signal sent to the child's process group: once the leader has
    been reaped the process-group id may be reused, so signalling it could hit an
    unrelated process. A present-but-unreadable ``/proc/<pid>/stat`` is treated as
    gone, which fails safe on signalling at the cost of a possibly unconfirmed
    stop; on hosts without ``/proc`` the check conservatively reports the leader as
    alive so the previous signalling behaviour is preserved.
    """
    if pid <= 0:
        return False
    stat = f"/proc/{pid}/stat"
    if not os.path.exists("/proc"):
        return True
    try:
        with open(stat, "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[-1].split()
    except OSError:
        return False
    return bool(fields) and fields[0] != b"Z"


async def terminate_process_group(
    process: Any, *, grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS
) -> bool:
    """Stop the process group; True only when the leader's exit is confirmed.

    Every signal is gated on the leader still being alive as far as
    ``leader_alive`` can tell, so the group is not signalled on the strength of a
    merely late reap notification: once the leader has been reaped its
    process-group id may be reused, and signalling it - even with ``SIGTERM`` -
    could hit an unrelated process. That
    also means a descendant which ignores ``SIGTERM`` and outlives a
    promptly-exiting leader is deliberately NOT chased; callers must report such
    survivors (``group_members``) instead of claiming they were cleaned up.

    On hosts without ``/proc`` the liveness check cannot be made, so it
    conservatively reports the leader as alive and the previous signalling
    behaviour applies unchanged.
    """
    if getattr(process, "returncode", None) is not None:
        return True
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return False

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not leader_alive(pid):
            # The leader is already gone (or a zombie); the reap notification is
            # merely late. Signalling the group now could hit a reused pgid.
            break
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            return False
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
            return True
        except asyncio.TimeoutError:
            continue

    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return True
    except asyncio.TimeoutError:
        return False


class NdjsonProcessTransport:
    """Framed NDJSON over a spawned CLI's stdio."""

    def __init__(
        self,
        process: SpawnedProcess,
        *,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES,
        grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    ) -> None:
        self._process = process
        self._max_frame_bytes = max_frame_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._grace_seconds = grace_seconds
        self._frames = FrameReader(process.stdout, max_frame_bytes=max_frame_bytes)
        self._stderr_bytes = 0
        self._stderr_tail = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())

    # ------------------------------------------------------------- lifecycle

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def exited(self) -> bool:
        return self._process.returncode is not None

    # ------------------------------------------------------------- protocol

    async def send(self, message: Mapping[str, Any]) -> None:
        from .ndjson import encode_frame

        writer = self._process.stdin
        writer.write(encode_frame(message))
        await writer.drain()

    async def recv(self) -> dict[str, Any] | None:
        """Next frame, or None on a clean EOF at a frame boundary."""
        return await self._frames.read_message()

    async def recv_within(self, timeout: float) -> dict[str, Any] | None:
        """Next frame under a bounded wait; raises TimeoutError on expiry."""
        return await asyncio.wait_for(self._frames.read_message(), timeout=timeout)

    # -------------------------------------------------------------- stderr

    async def _drain_stderr(self) -> None:
        """Keep only a bounded tail of stderr; never surface it as events."""
        stream = self._process.stderr
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                self._stderr_bytes += len(chunk)
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > self._max_stderr_bytes:
                    del self._stderr_tail[: len(self._stderr_tail) - self._max_stderr_bytes]
        except (asyncio.CancelledError, OSError):
            return

    def stderr_bytes_seen(self) -> int:
        return self._stderr_bytes

    def stderr_tail(self) -> str:
        return bytes(self._stderr_tail).decode("utf-8", "replace")

    def surviving_group_members(self) -> list[int]:
        """Group members still alive after termination (report, never signal)."""
        return group_members(self._process.pid)

    def stderr_classification(self) -> str:
        """Safe summary only - never the raw text, which may hold secrets."""
        if self._stderr_bytes == 0:
            return "no stderr output"
        if self._stderr_bytes > self._max_stderr_bytes:
            return f"stderr output present ({self._stderr_bytes} bytes, truncated)"
        return f"stderr output present ({self._stderr_bytes} bytes)"

    # ------------------------------------------------------------- shutdown

    async def terminate(self) -> bool:
        confirmed = await terminate_process_group(
            self._process, grace_seconds=self._grace_seconds
        )
        await self._stop_stderr_task()
        return confirmed

    async def _stop_stderr_task(self) -> None:
        task = self._stderr_task
        self._stderr_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown path
            pass

    async def aclose(self) -> bool:
        """Terminate (confirmed), then release the pipes.

        Returns whether termination was confirmed, so a caller can distinguish
        "the process is gone" from "the process may still be running".
        """
        confirmed = await self.terminate()
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001 - shutdown path
                    pass
        # asyncio keeps the read-pipe transports alive until EOF, and a surviving
        # descendant can hold the write end open indefinitely. Close the
        # subprocess transport itself (the only handle that releases every pipe)
        # so cleanup does not depend on the child's behaviour.
        inner = getattr(self._process, "_transport", None)
        inner_close = getattr(inner, "close", None)
        if inner_close is not None:
            try:
                inner_close()
            except Exception:  # noqa: BLE001 - shutdown path
                pass
        # asyncio defers the pipe connection_lost callbacks with call_soon; let
        # them run before the caller's event loop goes away, otherwise the
        # subprocess transport is collected later on a closed loop and raises
        # "Event loop is closed" from __del__.
        for _ in range(2):
            await asyncio.sleep(0)
        return confirmed
