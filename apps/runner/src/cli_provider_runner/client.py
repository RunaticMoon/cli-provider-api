"""Client for the Runner's private Unix-socket NDJSON RPC.

Used by tests and by the operator CLI. The next API slice can reuse this
directly instead of importing a driver.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, AsyncIterator, Mapping

from cli_provider_transports import (
    DEFAULT_MAX_FRAME_BYTES,
    FrameReader,
    decode_frame,
    encode_frame,
)

from .protocol import RunnerEventEnvelope, RunnerRequest, RunnerResponse


class RunnerClient:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    ) -> None:
        self._writer = writer
        self._max_frame_bytes = max_frame_bytes
        self._frames = FrameReader(reader, max_frame_bytes=max_frame_bytes)
        self.last_run_response: RunnerResponse | None = None

    @classmethod
    async def connect(
        cls, socket_path: str, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES
    ) -> "RunnerClient":
        reader, writer = await asyncio.open_unix_connection(
            socket_path, limit=max(max_frame_bytes * 2, 65536)
        )
        return cls(reader, writer, max_frame_bytes=max_frame_bytes)

    async def send_request(
        self, method: str, params: Mapping[str, Any] | None = None, request_id: str | None = None
    ) -> str:
        rid = request_id or uuid.uuid4().hex
        request = RunnerRequest(id=rid, method=method, params=dict(params or {}))
        self._writer.write(encode_frame(request.model_dump(mode="json")))
        await self._writer.drain()
        return rid

    async def recv(self) -> dict[str, Any] | None:
        line = await self._frames.read_bytes()
        if line is None:
            return None
        return decode_frame(line, self._max_frame_bytes)

    async def call(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> RunnerResponse:
        await self.send_request(method, params)
        while True:
            frame = await self.recv()
            if frame is None:
                raise ConnectionError("runner closed the connection")
            if frame.get("type") == "response":
                return RunnerResponse.model_validate(frame)

    async def run(self, params: Mapping[str, Any]) -> AsyncIterator[RunnerEventEnvelope]:
        self.last_run_response = None
        await self.send_request("run", params)
        while True:
            frame = await self.recv()
            if frame is None:
                raise ConnectionError("runner closed the connection during run")
            if frame.get("type") == "event":
                yield RunnerEventEnvelope.model_validate(frame)
                continue
            if frame.get("type") == "response":
                self.last_run_response = RunnerResponse.model_validate(frame)
                return

    def abort(self) -> None:
        """Abruptly drop the connection without a graceful close."""
        self._writer.transport.abort()

    async def aclose(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
