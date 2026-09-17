"""Bounded NDJSON codec.

One JSON object per line. Frames are size-bounded; malformed JSON is rejected
outright and never "repaired" or guessed at. A frame that cannot be parsed is a
protocol error, not something to salvage.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

DEFAULT_MAX_FRAME_BYTES = 1 << 20


class TransportError(Exception):
    """Base class for NDJSON transport failures."""


class MalformedFrame(TransportError):
    """A frame was not a single, complete, JSON object."""


class FrameTooLarge(TransportError):
    """A frame exceeded the configured bound."""


def encode_frame(message: Mapping[str, Any]) -> bytes:
    """Serialize one message to a single newline-terminated UTF-8 frame."""
    text = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8") + b"\n"


def decode_frame(line: bytes, max_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> Any:
    """Decode one frame body (without its newline). No repair, no coercion."""
    if len(line) > max_bytes:
        raise FrameTooLarge(f"frame of {len(line)} bytes exceeds bound {max_bytes}")
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedFrame("frame is not valid UTF-8") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedFrame(f"frame is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise MalformedFrame("frame must be a JSON object")
    return value


class FrameReader:
    """Incremental newline-delimited frame reader with a hard size bound."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        *,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        chunk_bytes: int = 4096,
    ) -> None:
        if max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes must be positive")
        self._reader = reader
        self._max = max_frame_bytes
        self._chunk = chunk_bytes
        self._buffer = bytearray()

    async def read_bytes(self) -> bytes | None:
        """Return the next frame body, or None on a clean EOF at a boundary."""
        while True:
            index = self._buffer.find(b"\n")
            if index != -1:
                line = bytes(self._buffer[:index])
                del self._buffer[: index + 1]
                if len(line) > self._max:
                    raise FrameTooLarge(
                        f"frame of {len(line)} bytes exceeds bound {self._max}"
                    )
                return line
            if len(self._buffer) > self._max:
                raise FrameTooLarge(
                    f"frame exceeds bound {self._max} before newline"
                )
            chunk = await self._reader.read(self._chunk)
            if not chunk:
                if not self._buffer:
                    return None
                raise MalformedFrame("stream ended with an unterminated frame")
            self._buffer.extend(chunk)

    async def read_message(self) -> Any | None:
        line = await self.read_bytes()
        if line is None:
            return None
        return decode_frame(line, self._max)
