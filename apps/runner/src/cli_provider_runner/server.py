"""Standalone Runner server over a private Unix-domain socket.

Responsibilities:
  * load exactly one allowlisted driver at startup and fail closed on a
    missing/invalid manifest;
  * expose versioned NDJSON RPC (see protocol.py);
  * validate the driver's event stream (run id, monotonic sequence, exactly one
    terminal event, no events after it);
  * enforce a finite execution deadline mid-stream (events cannot renew it) and
    bound the cancel deadline;
  * cancel a queued run without ever starting the driver;
  * report cancellation as requested vs confirmed;
  * keep per-instance concurrency at 1 with a bounded queue;
  * always release tasks/sockets in ``finally``.

The socket is a private local IPC endpoint. It is NOT an OS sandbox: process
separation here is not a security boundary.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from cli_provider_sdk import (
    CancelResult,
    CompletionStatus,
    EventKind,
    NormalizedRequest,
    Outcome,
    ProviderDriver,
    RunResult,
    RuntimeContext,
    SimpleCancellation,
    Usage,
    UsageProvenance,
    Verification,
    VerificationStatus,
    is_terminal_kind,
)
from cli_provider_transports import (
    DEFAULT_MAX_FRAME_BYTES,
    FrameReader,
    FrameTooLarge,
    MalformedFrame,
    decode_frame,
    encode_frame,
)

from .protocol import (
    CancelParams,
    ErrorCode,
    ErrorInfo,
    Method,
    RunParams,
    RunnerEventEnvelope,
    RunnerRequest,
    RunnerResponse,
    RunnerRuntime,
)
from .registry import DriverAllowlistEntry, DriverLoadError, load_driver, validate_manifest

DEFAULT_MAX_RUN_SECONDS = 300.0


@dataclass
class ActiveRun:
    run_id: str
    request: NormalizedRequest
    cancellation: SimpleCancellation = field(default_factory=SimpleCancellation)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    start_cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    terminal_kind: EventKind | None = None
    queued: bool = True
    started: bool = False
    cancel_before_start: bool = False
    deadline_exceeded: bool = False
    events_seen: int = 0
    last_sequence: int = 0


class RunnerServer:
    def __init__(
        self,
        *,
        socket_path: str,
        instance_id: str,
        entry: DriverAllowlistEntry | None = None,
        driver: ProviderDriver | None = None,
        max_queue: int = 8,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        cancel_deadline_seconds: float = 5.0,
        max_run_seconds: float = DEFAULT_MAX_RUN_SECONDS,
    ) -> None:
        if max_queue < 1:
            raise ValueError("max_queue must be at least 1")
        if (entry is None) == (driver is None):
            raise ValueError("provide exactly one of 'entry' or 'driver'")
        self.socket_path = socket_path
        self.instance_id = instance_id
        self.entry = entry
        self.max_queue = max_queue
        self.max_frame_bytes = max_frame_bytes
        self.cancel_deadline_seconds = cancel_deadline_seconds
        self.max_run_seconds = max_run_seconds

        self._driver: ProviderDriver | None = driver
        self._server: asyncio.AbstractServer | None = None
        self._stop = asyncio.Event()
        self._run_slot = asyncio.Semaphore(1)
        self._queue_waiting = 0
        self._active: dict[str, ActiveRun] = {}

    @property
    def driver(self) -> ProviderDriver:
        if self._driver is None:
            raise RuntimeError("driver not loaded; call load() first")
        return self._driver

    def load(self) -> ProviderDriver:
        """Resolve and validate the driver. Fails closed before running."""
        if self._driver is None:
            if self.entry is None:
                raise DriverLoadError(
                    "MANIFEST_INVALID", "no allowlist entry or driver supplied"
                )
            self._driver = load_driver(self.entry)
        else:
            validate_manifest(self._driver.manifest)
        return self._driver

    def request_stop(self) -> None:
        self._stop.set()

    async def serve(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        # Create the socket with restrictive permissions from the first instant:
        # the process umask is narrowed across the bind so the socket is never
        # group/world-connectable, even for a permissive caller umask. The chmod
        # below is only a belt-and-braces backstop, not the primary control.
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_connection,
                path=self.socket_path,
                limit=max(self.max_frame_bytes * 2, 65536),
            )
        finally:
            os.umask(old_umask)
        os.chmod(self.socket_path, 0o600)
        try:
            await self._stop.wait()
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for active in list(self._active.values()):
            active.cancellation.request()
            active.start_cancelled.set()
        if self._driver is not None:
            try:
                await self._driver.aclose()
            except Exception:
                pass
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass

    # ------------------------------------------------------------------ wire

    async def _safe_write(self, writer: asyncio.StreamWriter, message: dict) -> bool:
        try:
            writer.write(encode_frame(message))
            await writer.drain()
            return True
        except (ConnectionResetError, BrokenPipeError, OSError):
            return False

    async def _send_response(
        self, writer: asyncio.StreamWriter, request_id: str, result: dict
    ) -> bool:
        return await self._safe_write(
            writer,
            RunnerResponse(id=request_id, ok=True, result=result).model_dump(mode="json"),
        )

    async def _send_error(
        self,
        writer: asyncio.StreamWriter,
        request_id: str,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> bool:
        return await self._safe_write(
            writer,
            RunnerResponse(
                id=request_id,
                ok=False,
                error=ErrorInfo(code=code.value, message=message, retryable=retryable),
            ).model_dump(mode="json"),
        )

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        frames = FrameReader(reader, max_frame_bytes=self.max_frame_bytes)
        try:
            while True:
                try:
                    line = await frames.read_bytes()
                except FrameTooLarge as exc:
                    await self._send_error(writer, "", ErrorCode.FRAME_TOO_LARGE, str(exc))
                    break
                except MalformedFrame as exc:
                    await self._send_error(writer, "", ErrorCode.MALFORMED_REQUEST, str(exc))
                    break
                if line is None:
                    break
                try:
                    message = decode_frame(line, self.max_frame_bytes)
                    request = RunnerRequest.model_validate(message)
                except (MalformedFrame, FrameTooLarge) as exc:
                    await self._send_error(
                        writer, "", ErrorCode.MALFORMED_REQUEST, str(exc)
                    )
                    break
                except ValidationError:
                    await self._send_error(
                        writer, "", ErrorCode.MALFORMED_REQUEST, "invalid request envelope"
                    )
                    continue
                if not await self._dispatch(request, writer):
                    break
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass

    async def _dispatch(self, request: RunnerRequest, writer: asyncio.StreamWriter) -> bool:
        try:
            method = Method(request.method)
        except ValueError:
            await self._send_error(
                writer,
                request.id,
                ErrorCode.UNKNOWN_METHOD,
                f"unknown method {request.method!r}",
            )
            return True

        if method is Method.SHUTDOWN:
            await self._send_response(
                writer, request.id, {"instance_id": self.instance_id, "stopping": True}
            )
            self.request_stop()
            return True

        if method is Method.MANIFEST:
            await self._send_response(
                writer, request.id, self.driver.manifest.model_dump(mode="json")
            )
            return True

        if method is Method.PROBE:
            probe = await self.driver.probe(RuntimeContext())
            await self._send_response(writer, request.id, probe.model_dump(mode="json"))
            return True

        if method is Method.DISCOVER_MODELS:
            models = await self.driver.discover_models(RuntimeContext())
            await self._send_response(
                writer,
                request.id,
                {"models": [model.model_dump(mode="json") for model in models]},
            )
            return True

        if method is Method.RUNTIME:
            # Runner-owned capacity: one execution slot plus a bounded queue.
            runtime = RunnerRuntime(
                max_parallel_runs=1,
                max_queue=self.max_queue,
                cancel_cleanup_seconds=2.0 * self.cancel_deadline_seconds,
            )
            await self._send_response(
                writer, request.id, runtime.model_dump(mode="json")
            )
            return True

        if method is Method.RUN:
            await self._handle_run(request, writer)
            return True

        if method is Method.CANCEL:
            await self._handle_cancel(request, writer)
            return True

        return True

    # ------------------------------------------------------------------- run

    def _effective_deadline(self, request: NormalizedRequest) -> float:
        requested = request.deadline_seconds or self.max_run_seconds
        return min(requested, self.max_run_seconds)

    async def _handle_run(
        self, request: RunnerRequest, writer: asyncio.StreamWriter
    ) -> None:
        try:
            params = RunParams.model_validate(request.params)
        except ValidationError as exc:
            await self._send_error(
                writer,
                request.id,
                ErrorCode.INVALID_PARAMS,
                f"invalid run params: {exc.error_count()} problem(s)",
            )
            return

        driver_request = params.to_driver_request()
        run_id = driver_request.run_id

        if run_id in self._active:
            await self._send_error(
                writer,
                request.id,
                ErrorCode.RUN_ALREADY_ACTIVE,
                f"run {run_id!r} is already active",
            )
            return

        if self._run_slot.locked() and self._queue_waiting >= self.max_queue:
            await self._send_error(
                writer,
                request.id,
                ErrorCode.QUEUE_FULL,
                f"queue full ({self._queue_waiting}/{self.max_queue} waiting)",
                retryable=True,
            )
            return

        active = ActiveRun(run_id=run_id, request=driver_request)
        self._active[run_id] = active
        acquired = False
        try:
            self._queue_waiting += 1
            acquire_task = asyncio.ensure_future(self._run_slot.acquire())
            cancel_wait = asyncio.ensure_future(active.start_cancelled.wait())
            try:
                done, _ = await asyncio.wait(
                    {acquire_task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                self._queue_waiting -= 1
                if not cancel_wait.done():
                    cancel_wait.cancel()

            if acquire_task in done and not acquire_task.cancelled():
                acquired = bool(acquire_task.result())
            else:
                acquire_task.cancel()
                try:
                    await acquire_task
                except BaseException:
                    pass
                if acquire_task.done() and not acquire_task.cancelled():
                    acquired = bool(acquire_task.result())

            if active.cancel_before_start:
                await self._send_response(
                    writer,
                    request.id,
                    self._cancelled_before_start_result(active).model_dump(mode="json"),
                )
                return

            active.queued = False
            active.started = True
            result = await self._run_with_deadline(active, writer, request.id)
            await self._send_response(writer, request.id, result.model_dump(mode="json"))
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        except Exception as exc:  # pragma: no cover - defensive
            await self._send_error(
                writer, request.id, ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
            )
        finally:
            self._active.pop(run_id, None)
            active.finished.set()
            if acquired:
                self._run_slot.release()

    async def _run_with_deadline(
        self, active: ActiveRun, writer: asyncio.StreamWriter, request_id: str
    ) -> RunResult:
        deadline = self._effective_deadline(active.request)
        consumer = asyncio.create_task(self._consume(active, writer, request_id))
        done, _ = await asyncio.wait({consumer}, timeout=deadline)
        if consumer in done:
            exc = consumer.exception()
            if exc is not None:
                raise exc
            return consumer.result()

        # Deadline exceeded. Events/keepalives cannot renew it; stop consuming,
        # ask the driver to stop within a bounded budget, then abandon.
        active.deadline_exceeded = True
        active.cancellation.request()
        confirmed, detail = await self._bounded_driver_cancel(active.run_id)
        consumer.cancel()
        stopped, _ = await asyncio.wait({consumer}, timeout=self.cancel_deadline_seconds)
        if consumer not in stopped:
            return self._unknown_result(
                active,
                reason="execution deadline exceeded; driver did not stop within cancel deadline",
            )
        if confirmed:
            return self._deadline_cancelled_result(active, detail=detail)
        return self._unknown_result(
            active,
            reason="execution deadline exceeded; cancellation was not confirmed",
        )

    async def _consume(
        self, active: ActiveRun, writer: asyncio.StreamWriter, request_id: str
    ) -> RunResult:
        context = RuntimeContext(cancellation=active.cancellation)
        terminal: Any = None
        violation: str | None = None

        iterator = self.driver.execute(active.request, context)
        try:
            async for event in iterator:
                active.events_seen += 1
                if event.run_id != active.run_id:
                    violation = (
                        f"event run_id {event.run_id!r} != request run_id {active.run_id!r}"
                    )
                    break
                if terminal is not None:
                    violation = "driver emitted an event after its terminal event"
                    break
                if event.sequence != active.last_sequence + 1:
                    violation = (
                        f"non-monotonic event sequence: expected "
                        f"{active.last_sequence + 1}, got {event.sequence}"
                    )
                    break
                active.last_sequence = event.sequence
                sent = await self._safe_write(
                    writer,
                    RunnerEventEnvelope(request_id=request_id, event=event).model_dump(
                        mode="json"
                    ),
                )
                if not sent:
                    violation = "client disconnected during stream"
                    break
                if is_terminal_kind(event.kind):
                    terminal = event
        except (ConnectionResetError, BrokenPipeError, OSError):
            violation = "client disconnected during stream"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            violation = f"driver stopped unexpectedly: {type(exc).__name__}: {exc}"
        finally:
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass

        if violation is not None:
            return self._unknown_result(active, reason=violation)
        if terminal is None:
            return self._unknown_result(
                active, reason="driver stream ended without a terminal event"
            )

        kind = EventKind(terminal.kind)
        active.terminal_kind = kind
        if kind is EventKind.RUN_COMPLETED:
            # A completed run is not automatically a successful one.
            status = CompletionStatus.COMPLETED
            outcome = Outcome(terminal.payload.outcome)
            usage = terminal.payload.usage
        elif kind is EventKind.RUN_FAILED:
            status, outcome = CompletionStatus.FAILED, Outcome.PROVIDER_ERROR
            usage = Usage(provenance=UsageProvenance.UNKNOWN)
        else:
            status, outcome = CompletionStatus.CANCELLED, Outcome.CANCELLED
            usage = Usage(provenance=UsageProvenance.UNKNOWN)

        return RunResult(
            run_id=active.run_id,
            status=status,
            outcome=outcome,
            verification=Verification(
                status=VerificationStatus.NOT_RUN,
                source="driver",
                reason="driver reported a terminal event; no independent verification ran",
            ),
            usage=usage,
            terminal_kind=kind,
            terminal_sequence=terminal.sequence,
            events_seen=active.events_seen,
            synthetic=self.driver.manifest.synthetic,
        )

    def _unknown_result(self, active: ActiveRun, *, reason: str) -> RunResult:
        active.terminal_kind = None
        return RunResult(
            run_id=active.run_id,
            status=CompletionStatus.UNKNOWN,
            outcome=Outcome.UNKNOWN,
            verification=Verification(
                status=VerificationStatus.UNKNOWN, source="runner", reason=reason
            ),
            usage=Usage(provenance=UsageProvenance.UNKNOWN),
            terminal_kind=None,
            terminal_sequence=None,
            events_seen=active.events_seen,
            synthetic=self.driver.manifest.synthetic,
            detail=reason,
        )

    def _cancelled_before_start_result(self, active: ActiveRun) -> RunResult:
        return RunResult(
            run_id=active.run_id,
            status=CompletionStatus.CANCELLED,
            outcome=Outcome.CANCELLED,
            verification=Verification(
                status=VerificationStatus.NOT_RUN,
                source="runner",
                reason="cancelled while queued; driver was never started",
            ),
            usage=Usage(provenance=UsageProvenance.UNKNOWN),
            terminal_kind=None,
            terminal_sequence=None,
            events_seen=0,
            synthetic=self.driver.manifest.synthetic,
            detail="cancelled before driver start",
        )

    def _deadline_cancelled_result(
        self, active: ActiveRun, *, detail: str | None = None
    ) -> RunResult:
        return RunResult(
            run_id=active.run_id,
            status=CompletionStatus.CANCELLED,
            outcome=Outcome.CANCELLED,
            verification=Verification(
                status=VerificationStatus.NOT_RUN,
                source="runner",
                reason="execution deadline exceeded; driver stop confirmed",
            ),
            usage=Usage(provenance=UsageProvenance.UNKNOWN),
            terminal_kind=None,
            terminal_sequence=None,
            events_seen=active.events_seen,
            synthetic=self.driver.manifest.synthetic,
            # Keep the driver's own bounded detail when it has one: it is where a
            # deliberately-unstopped descendant is reported.
            detail=(detail[:200] if detail else "execution deadline exceeded"),
        )

    # ---------------------------------------------------------------- cancel

    async def _bounded_driver_cancel(self, run_id: str) -> tuple[bool, str | None]:
        try:
            result = await asyncio.wait_for(
                self.driver.cancel(run_id, RuntimeContext()),
                timeout=self.cancel_deadline_seconds,
            )
            return result.confirmed, result.detail
        except asyncio.TimeoutError:
            return False, "driver cancel call exceeded deadline"

    async def _handle_cancel(
        self, request: RunnerRequest, writer: asyncio.StreamWriter
    ) -> None:
        try:
            params = CancelParams.model_validate(request.params)
        except ValidationError as exc:
            await self._send_error(
                writer,
                request.id,
                ErrorCode.INVALID_PARAMS,
                f"invalid cancel params: {exc.error_count()} problem(s)",
            )
            return

        active = self._active.get(params.run_id)
        now = datetime.now(timezone.utc)
        if active is None:
            await self._send_response(
                writer,
                request.id,
                CancelResult(
                    run_id=params.run_id,
                    requested=False,
                    requested_at=now,
                    confirmed=False,
                    deadline_seconds=self.cancel_deadline_seconds,
                    detail="no active run for this id",
                ).model_dump(mode="json"),
            )
            return

        if active.queued and not active.started:
            # Cancelling a queued run must not start the driver at all.
            active.cancel_before_start = True
            active.start_cancelled.set()
            confirmed = False
            try:
                await asyncio.wait_for(
                    active.finished.wait(), timeout=self.cancel_deadline_seconds
                )
                confirmed = not active.started
            except asyncio.TimeoutError:
                confirmed = False
            await self._send_response(
                writer,
                request.id,
                CancelResult(
                    run_id=params.run_id,
                    requested=True,
                    requested_at=now,
                    confirmed=confirmed,
                    confirmed_at=datetime.now(timezone.utc) if confirmed else None,
                    deadline_seconds=self.cancel_deadline_seconds,
                    detail=(
                        "cancelled before driver start; driver never executed"
                        if confirmed
                        else "queued cancellation was not confirmed within deadline"
                    ),
                ).model_dump(mode="json"),
            )
            return

        active.cancellation.request()
        driver_confirmed, driver_detail = await self._bounded_driver_cancel(params.run_id)

        confirmed = False
        try:
            await asyncio.wait_for(
                active.finished.wait(), timeout=self.cancel_deadline_seconds
            )
            confirmed = active.terminal_kind is EventKind.RUN_CANCELLED
        except asyncio.TimeoutError:
            confirmed = False

        details = []
        if driver_detail:
            details.append(driver_detail)
        if not driver_confirmed:
            details.append("driver did not confirm cancellation")
        details.append(
            "termination confirmed"
            if confirmed
            else "termination not confirmed within deadline"
        )
        await self._send_response(
            writer,
            request.id,
            CancelResult(
                run_id=params.run_id,
                requested=True,
                requested_at=now,
                confirmed=confirmed,
                confirmed_at=datetime.now(timezone.utc) if confirmed else None,
                deadline_seconds=self.cancel_deadline_seconds,
                detail="; ".join(details),
            ).model_dump(mode="json"),
        )
