"""Minimal stdio transport: protocol on stdout, diagnostics on stderr.

This is a plain length-bounded NDJSON transport. It makes no claim about PTY or
ACP support; those are separate, not-yet-implemented transports.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Protocol

from .ndjson import DEFAULT_MAX_FRAME_BYTES, FrameReader, decode_frame, encode_frame


class AsyncByteWriter(Protocol):
    def write(self, data: bytes) -> Any: ...

    async def drain(self) -> None: ...


class AsyncTextWriter(Protocol):
    def write(self, text: str) -> Any: ...


class StdioTransport:
    def __init__(
        self,
        reader: asyncio.StreamReader | None,
        writer: AsyncByteWriter,
        *,
        stderr_writer: AsyncTextWriter | None = None,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    ) -> None:
        self._writer = writer
        self._stderr = stderr_writer
        self._max = max_frame_bytes
        self._frames = (
            FrameReader(reader, max_frame_bytes=max_frame_bytes)
            if reader is not None
            else None
        )

    async def send(self, message: Mapping[str, Any]) -> None:
        self._writer.write(encode_frame(message))
        await self._writer.drain()

    async def recv(self) -> Any | None:
        if self._frames is None:
            raise RuntimeError("transport has no read side")
        line = await self._frames.read_bytes()
        if line is None:
            return None
        return decode_frame(line, self._max)

    def log_stderr(self, message: str) -> None:
        """Diagnostics go to stderr, never into the protocol stream."""
        if self._stderr is not None:
            self._stderr.write(message + "\n")

    async def aclose(self) -> None:
        close = getattr(self._writer, "close", None)
        if close is not None:
            close()
        wait_closed = getattr(self._writer, "wait_closed", None)
        if wait_closed is not None:
            await wait_closed()
