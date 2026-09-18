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
from .process import (
    DEFAULT_MAX_STDERR_BYTES,
    DEFAULT_TERMINATION_GRACE_SECONDS,
    LocalProcessExecutor,
    NdjsonProcessTransport,
    ProcessStartError,
    SpawnedProcess,
    group_members,
    leader_alive,
    terminate_process_group,
)
from .stdio import StdioTransport

__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "DEFAULT_MAX_STDERR_BYTES",
    "DEFAULT_TERMINATION_GRACE_SECONDS",
    "FrameReader",
    "FrameTooLarge",
    "LocalProcessExecutor",
    "MalformedFrame",
    "NdjsonProcessTransport",
    "ProcessStartError",
    "SpawnedProcess",
    "StdioTransport",
    "TransportError",
    "decode_frame",
    "encode_frame",
    "group_members",
    "leader_alive",
    "terminate_process_group",
]
