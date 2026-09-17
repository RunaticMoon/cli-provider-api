"""Bounded NDJSON transport primitives."""

from .ndjson import (
    DEFAULT_MAX_FRAME_BYTES,
    FrameReader,
    FrameTooLarge,
    MalformedFrame,
    TransportError,
    decode_frame,
    encode_frame,
)
from .stdio import StdioTransport

__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "FrameReader",
    "FrameTooLarge",
    "MalformedFrame",
    "StdioTransport",
    "TransportError",
    "decode_frame",
    "encode_frame",
]
