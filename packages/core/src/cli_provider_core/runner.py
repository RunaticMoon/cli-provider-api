"""Validated client session to a Runner over the bounded NDJSON UDS transport.

This is the only way core/API talk to a Runner. It validates protocol version,
matching request ID / run ID, event ordering and terminal schema, and never
accepts an unrelated response. It never imports or loads a driver.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Mapping, Protocol, runtime_checkable

from cli_provider_runner.client import RunnerClient
from cli_provider_runner.protocol import (
    RunnerEventEnvelope,
    RunnerResponse,
    RunnerRuntime,
)
from cli_provider_sdk import (
    CancelResult,
    DriverManifest,
    ModelDescriptor,
    ProbeReport,
    RunResult,
)
from pydantic import ValidationError

from .errors import RunnerRunRejected, UpstreamProtocolError

PROTOCOL_VERSION = 1

# Fallback only when neither an operator override nor a run deadline is known.
DEFAULT_RUN_FRAME_SECONDS = 60.0


@runtime_checkable
class RunnerSession(Protocol):
    async def manifest(self) -> dict[str, Any]: ...
    async def probe(self) -> dict[str, Any]: ...
    async def discover_models(self) -> list[dict[str, Any]]: ...
    async def runtime(self) -> dict[str, Any]: ...
    def run(self, params: Mapping[str, Any]) -> AsyncIterator[dict[str, Any]]: ...
    async def cancel(self, run_id: str) -> CancelResult: ...
    async def aclose(self) -> None: ...


class UdsRunnerSession:
    def __init__(
        self,
        socket_path: str,
        *,
        connect_timeout_seconds: float = 5.0,
        call_timeout_seconds: float = 5.0,
        run_timeout_seconds: float | None = None,
        max_frame_bytes: int | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.connect_timeout_seconds = connect_timeout_seconds
        # Control RPCs (manifest/probe/discover_models/cancel) stay short.
        self.call_timeout_seconds = call_timeout_seconds
        # Streaming runs are bounded by the run's actual deadline/cancel budget.
        self.run_timeout_seconds = run_timeout_seconds
        self.max_frame_bytes = max_frame_bytes
        self.last_result: RunResult | None = None
        self._client: RunnerClient | None = None

    def set_run_timeout(self, seconds: float) -> None:
        self.run_timeout_seconds = seconds

    def _run_recv_timeout(self, params: Mapping[str, Any]) -> float:
        if self.run_timeout_seconds is not None:
            return self.run_timeout_seconds
        declared = params.get("deadline_seconds")
        base = float(declared) if declared else DEFAULT_RUN_FRAME_SECONDS
        # Allow for the cancel budget the controller adds around the deadline.
        return base + 5.0

    async def _connect(self) -> RunnerClient:
        if self._client is not None:
            return self._client
        kwargs: dict[str, Any] = {}
        if self.max_frame_bytes is not None:
            kwargs["max_frame_bytes"] = self.max_frame_bytes
        try:
            self._client = await asyncio.wait_for(
                RunnerClient.connect(self.socket_path, **kwargs),
                timeout=self.connect_timeout_seconds,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise UpstreamProtocolError(f"runner unavailable at {self.socket_path}") from exc
        return self._client

    def _check_version(self, frame: dict[str, Any]) -> None:
        if int(frame.get("v", -1)) != PROTOCOL_VERSION:
            raise UpstreamProtocolError("runner protocol version mismatch")

    async def _request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        client = await self._connect()
        rid = await client.send_request(method, params)
        while True:
            try:
                frame = await asyncio.wait_for(
                    client.recv(), timeout=self.call_timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                raise UpstreamProtocolError("runner response timed out") from exc
            if frame is None:
                raise UpstreamProtocolError("runner closed the connection")
            self._check_version(frame)
            if frame.get("type") != "response":
                raise UpstreamProtocolError("runner sent an unexpected frame")
            response = RunnerResponse.model_validate(frame)
            if response.id != rid:
                raise UpstreamProtocolError("runner response ID does not match request")
            if not response.ok:
                code = response.error.code if response.error else "unknown"
                message = response.error.message if response.error else "runner error"
                raise UpstreamProtocolError(f"runner error {code}: {message}")
            return response.result or {}

    async def manifest(self) -> dict[str, Any]:
        result = await self._request("manifest")
        try:
            return DriverManifest.model_validate(result).model_dump(mode="json")
        except ValidationError as exc:
            raise UpstreamProtocolError(
                "runner manifest failed SDK schema validation"
            ) from exc

    async def probe(self) -> dict[str, Any]:
        result = await self._request("probe")
        try:
            return ProbeReport.model_validate(result).model_dump(mode="json")
        except ValidationError as exc:
            raise UpstreamProtocolError(
                "runner probe failed SDK schema validation"
            ) from exc

    async def discover_models(self) -> list[dict[str, Any]]:
        result = await self._request("discover_models")
        models = result.get("models")
        if not isinstance(models, list):
            raise UpstreamProtocolError("discover_models returned no model list")
        validated: list[dict[str, Any]] = []
        for model in models:
            try:
                validated.append(
                    ModelDescriptor.model_validate(model).model_dump(mode="json")
                )
            except ValidationError as exc:
                raise UpstreamProtocolError(
                    "runner model descriptor failed SDK schema validation"
                ) from exc
        return validated

    async def runtime(self) -> dict[str, Any]:
        result = await self._request("runtime")
        try:
            return RunnerRuntime.model_validate(result).model_dump(mode="json")
        except ValidationError as exc:
            raise UpstreamProtocolError(
                "runner runtime failed schema validation"
            ) from exc

    async def run(self, params: Mapping[str, Any]) -> AsyncIterator[dict[str, Any]]:
        self.last_result = None
        run_id = str(params["run_id"])
        recv_timeout = self._run_recv_timeout(params)
        client = await self._connect()
        rid = await client.send_request("run", params)
        expected_sequence = 1
        saw_event = False
        while True:
            try:
                frame = await asyncio.wait_for(
                    client.recv(), timeout=recv_timeout
                )
            except asyncio.TimeoutError as exc:
                raise UpstreamProtocolError("runner stream timed out") from exc
            if frame is None:
                raise UpstreamProtocolError("runner closed the connection during run")
            self._check_version(frame)
            frame_type = frame.get("type")
            if frame_type == "event":
                try:
                    envelope = RunnerEventEnvelope.model_validate(frame)
                except ValidationError as exc:
                    raise UpstreamProtocolError("runner event failed schema validation") from exc
                if envelope.request_id != rid:
                    raise UpstreamProtocolError("event request ID does not match request")
                if envelope.event.run_id != run_id:
                    raise UpstreamProtocolError("event run ID does not match request")
                if envelope.event.sequence != expected_sequence:
                    raise UpstreamProtocolError("event sequence is not contiguous")
                expected_sequence += 1
                saw_event = True
                yield envelope.event.model_dump(mode="json")
                continue
            if frame_type == "response":
                try:
                    response = RunnerResponse.model_validate(frame)
                except ValidationError as exc:
                    raise UpstreamProtocolError("runner response failed schema validation") from exc
                if response.id != rid:
                    raise UpstreamProtocolError("run response ID does not match request")
                if not response.ok:
                    code = response.error.code if response.error else "unknown"
                    retryable = bool(response.error.retryable) if response.error else False
                    # Preserve the Runner's typed code/retryable/stage. An error
                    # before any event is a proven pre-execution rejection; after
                    # events it may have had an execution effect.
                    raise RunnerRunRejected(
                        f"runner run error {code}",
                        runner_code=code,
                        retryable=retryable,
                        stage="execution" if saw_event else "pre_execution",
                    )
                self.last_result = RunResult.model_validate(response.result)
                return
            raise UpstreamProtocolError("runner sent an unexpected frame")

    async def cancel(self, run_id: str) -> CancelResult:
        result = await self._request("cancel", {"run_id": run_id})
        return CancelResult.model_validate(result)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
