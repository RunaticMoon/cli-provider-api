"""Fixture Runner stand-in for 9Router retry-safety integration tests.

A real Unix-domain-socket NDJSON server speaking Runner protocol v1 — the same
wire the production Runner uses — so the API/core path under test is exercised
end to end (HTTP -> controller -> Store -> UDS -> worker fixture). It is NOT the
Runner implementation: it exists so tests can deterministically script pre/post
execution behaviour that the stock Runner's fixed mock driver cannot express,
and so it can accept the optional ``execution`` context that this slice adds to
``NormalizedRequest`` (Runner-side ``RunParams`` support is owned by another
worker; see docs/9ROUTER_NATIVE_SAFETY.md).

Usage:
    python fixture_runner.py --socket PATH --root DIR --instance-id ID

Behaviour plan lives in ``<root>/plan.json``::

    {"presets": {"fixture/alpha": ["reject", "success"]}, "default": "success"}

A preset maps to a queue of behaviours consumed one per ``run`` call; an empty
queue falls back to ``default``. Behaviours:

    reject            typed QUEUE_FULL error before any event (proven
                      pre-execution rejection; no agent effect)
    success           run.started + deltas + run.completed(succeeded)
    partial           run.started + deltas + run.completed(partial)
    fail              agent effect, run.started, then run.failed terminal
    error_after_event agent effect, run.started, then a typed INTERNAL_ERROR
                      response (execution-stage transport failure)
    drop              agent effect, run.started, then the connection is
                      abandoned mid-run (response dropped)
    hang              agent effect, run.started, then waits for cancel and
                      ends with run.cancelled
    hang_ignore       agent effect, run.started, then never finishes and
                      ignores cancel (unknown outcome)

Evidence files under ``<root>``:

    runs.ndjson    one record per ``run`` RPC received, with the verbatim
                   params (including ``execution``) and whether the fixture
                   dispatched the agent
    effects.ndjson one record per *agent start* — the synthetic file effect.
                   This file is the proof of execution: ``reject`` never
                   appends to it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from cli_provider_sdk import (
    CancelResult,
    CompletionStatus,
    EventKind,
    NormalizedRequest,
    Outcome,
    RunResult,
    Usage,
    UsageProvenance,
    Verification,
    VerificationStatus,
)

SDK_VERSION = "1.0"
DRIVER_ID = "fixture"
DRIVER_VERSION = "0.1.0"
MODEL_ID = "fixture-model"

REJECT_BEHAVIOR = "reject"
EFFECT_BEHAVIORS = frozenset(
    {
        "success",
        "partial",
        "fail",
        "error_after_event",
        "drop",
        "hang",
        "hang_ignore",
    }
)

MANIFEST = {
    "driver_id": DRIVER_ID,
    "name": "9Router safety fixture (synthetic)",
    "version": DRIVER_VERSION,
    "sdk_version": SDK_VERSION,
    "protocol_family": "fixture",
    "supported_transports": ["stdio"],
    "synthetic": True,
}
PROBE = {
    "ok": True,
    "driver_id": DRIVER_ID,
    "driver_version": DRIVER_VERSION,
    "cli_version": None,
    "capabilities": {
        "streaming": "native",
        "sessions": "none",
        "roles": "serialized",
        "structured_output": "none",
        "external_tool_calls": False,
        "internal_tools": False,
        "vision": False,
        "workspace_write": False,
        "web_search": False,
        "usage": "unknown",
    },
    "notes": ["synthetic retry-safety fixture; no real CLI or provider"],
}
MODELS = {
    "models": [
        {
            "model_id": MODEL_ID,
            "display_name": "Fixture Model (synthetic)",
            "verification": {
                "status": "unknown",
                "source": "fixture",
                "reason": "synthetic fixture; no real verification performed",
            },
            # Declared fixture effort support: proves the request-level
            # reasoning_effort carrier reaches the worker verbatim.
            "effort": "selectable",
            "effort_options": ["low", "medium", "high"],
        }
    ]
}
RUNTIME = {
    "max_parallel_runs": 2,
    "max_queue": 8,
    "cancel_cleanup_seconds": 1.0,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _frame(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")


async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> bool:
    try:
        _frame(writer, message)
        await writer.drain()
        return True
    except (ConnectionResetError, BrokenPipeError, OSError):
        return False


def _response(request_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"v": 1, "type": "response", "id": request_id, "ok": True, "result": result}


def _error(request_id: str, code: str, message: str, *, retryable: bool = False) -> dict:
    return {
        "v": 1,
        "type": "response",
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": message, "retryable": retryable},
    }


def _event(request_id: str, event: dict[str, Any]) -> dict[str, Any]:
    return {"v": 1, "type": "event", "request_id": request_id, "event": event}


class FixtureRunner:
    def __init__(self, socket_path: str, root: str, instance_id: str) -> None:
        self.socket_path = socket_path
        self.root = root
        self.instance_id = instance_id
        self._counters: dict[str, int] = {}
        self._cancel: dict[str, asyncio.Event] = {}
        self._server: asyncio.AbstractServer | None = None

    def _load_plan(self) -> dict[str, Any]:
        path = os.path.join(self.root, "plan.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        return {"default": "success"}

    def _behavior_for(self, preset: str) -> str:
        # The plan is re-read per call so a test can flip behaviour between
        # requests (e.g. first call succeeds, a later task fails).
        plan = self._load_plan()
        presets = plan.get("presets") or {}
        queue = presets.get(preset)
        if queue:
            index = self._counters.get(preset, 0)
            self._counters[preset] = index + 1
            if index < len(queue):
                return str(queue[index])
            return str(queue[-1])
        return str(plan.get("default", "success"))

    def _spool(self, name: str, record: dict[str, Any]) -> None:
        with open(os.path.join(self.root, name), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # --------------------------------------------------------------- events

    def _event(self, run_id: str, sequence: int, kind: str, payload: dict) -> dict:
        return {
            "schema_version": 1,
            "run_id": run_id,
            "sequence": sequence,
            "timestamp": _now(),
            "synthetic": True,
            "kind": kind,
            "payload": payload,
        }

    def _result(
        self,
        run_id: str,
        status: CompletionStatus,
        outcome: Outcome,
        events_seen: int,
        terminal_kind: EventKind | None,
        terminal_sequence: int | None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        return RunResult(
            run_id=run_id,
            status=status,
            outcome=outcome,
            verification=Verification(
                status=VerificationStatus.NOT_RUN,
                source="fixture",
                reason="fixture terminal; no independent verification ran",
            ),
            usage=Usage(provenance=UsageProvenance.UNKNOWN),
            terminal_kind=terminal_kind,
            terminal_sequence=terminal_sequence,
            events_seen=events_seen,
            synthetic=True,
            detail=detail,
        ).model_dump(mode="json")

    # ----------------------------------------------------------------- runs

    async def _run(
        self, request_id: str, params: dict[str, Any], writer: asyncio.StreamWriter
    ) -> None:
        try:
            run = NormalizedRequest.model_validate(params)
        except ValidationError:
            await _send(
                writer,
                _error(request_id, "INVALID_PARAMS", "invalid run params"),
            )
            return

        behavior = self._behavior_for(run.preset)
        if behavior == REJECT_BEHAVIOR:
            # Typed pre-execution rejection: the agent is provably never
            # dispatched, no effect line is written.
            self._spool(
                "runs.ndjson",
                {
                    "run_id": run.run_id,
                    "attempt_id": run.attempt_id,
                    "task_id": run.task_id,
                    "preset": run.preset,
                    "workspace": run.workspace.model_dump(mode="json"),
                    "execution": (
                        run.execution.model_dump(mode="json")
                        if run.execution is not None
                        else None
                    ),
                    "reasoning_effort": run.reasoning_effort,
                    "resolved_model": run.resolved_model,
                    "behavior": behavior,
                    "dispatched": False,
                    "received_at": _now(),
                },
            )
            await _send(
                writer,
                _error(
                    request_id,
                    "QUEUE_FULL",
                    "fixture candidate unavailable before dispatch",
                    retryable=True,
                ),
            )
            return

        sequence = 0
        run_id = run.run_id
        cancel_event = asyncio.Event()
        self._cancel[run_id] = cancel_event
        try:
            # The agent starts: the synthetic side effect is the durable proof
            # that execution began, recorded before the first event frame.
            self._spool(
                "effects.ndjson",
                {
                    "run_id": run_id,
                    "attempt_id": run.attempt_id,
                    "task_id": run.task_id,
                    "preset": run.preset,
                    "execution": (
                        run.execution.model_dump(mode="json")
                        if run.execution is not None
                        else None
                    ),
                    "reasoning_effort": run.reasoning_effort,
                    "resolved_model": run.resolved_model,
                    "behavior": behavior,
                    "started_at": _now(),
                },
            )
            self._spool(
                "runs.ndjson",
                {
                    "run_id": run_id,
                    "attempt_id": run.attempt_id,
                    "task_id": run.task_id,
                    "preset": run.preset,
                    "workspace": run.workspace.model_dump(mode="json"),
                    "execution": (
                        run.execution.model_dump(mode="json")
                        if run.execution is not None
                        else None
                    ),
                    "reasoning_effort": run.reasoning_effort,
                    "resolved_model": run.resolved_model,
                    "behavior": behavior,
                    "dispatched": True,
                    "received_at": _now(),
                },
            )
            sequence += 1
            if not await _send(
                writer,
                _event(
                    request_id,
                    self._event(
                        run_id,
                        sequence,
                        "run.started",
                        {"preset": run.preset, "model_alias": run.model_alias},
                    ),
                ),
            ):
                return

            if behavior == "hang_ignore":
                # Never finish, never honour cancel: the API's bounded budget
                # abandons the stream and must hold the outcome unknown.
                await asyncio.Event().wait()
                return

            if behavior == "hang":
                await cancel_event.wait()
                sequence += 1
                await _send(
                    writer,
                    _event(
                        request_id,
                        self._event(
                            run_id,
                            sequence,
                            "run.cancelled",
                            {"reason": "fixture cancel"},
                        ),
                    ),
                )
                await _send(
                    writer,
                    _response(
                        request_id,
                        self._result(
                            run_id,
                            CompletionStatus.CANCELLED,
                            Outcome.CANCELLED,
                            events_seen=sequence,
                            terminal_kind=EventKind.RUN_CANCELLED,
                            terminal_sequence=sequence,
                        ),
                    ),
                )
                return

            if behavior == "drop":
                # Transport dies after the agent started, before any terminal:
                # the API cannot learn the outcome and must hold unknown.
                writer.transport.abort()
                return

            if behavior == "error_after_event":
                await _send(
                    writer,
                    _error(
                        request_id,
                        "INTERNAL_ERROR",
                        "fixture transport failure after execution began",
                        retryable=True,
                    ),
                )
                return

            for chunk in ("[fixture] ", f"task:{run.task_id}", " done"):
                sequence += 1
                if not await _send(
                    writer,
                    _event(
                        request_id,
                        self._event(
                            run_id, sequence, "message.delta", {"text": chunk}
                        ),
                    ),
                ):
                    return

            if behavior == "fail":
                sequence += 1
                await _send(
                    writer,
                    _event(
                        request_id,
                        self._event(
                            run_id,
                            sequence,
                            "run.failed",
                            {
                                "code": "fixture_explicit_failure",
                                "message": "fixture agent failed after its effect",
                            },
                        ),
                    ),
                )
                await _send(
                    writer,
                    _response(
                        request_id,
                        self._result(
                            run_id,
                            CompletionStatus.FAILED,
                            Outcome.PROVIDER_ERROR,
                            events_seen=sequence,
                            terminal_kind=EventKind.RUN_FAILED,
                            terminal_sequence=sequence,
                            detail="fixture agent failed after its effect",
                        ),
                    ),
                )
                return

            outcome = "partial" if behavior == "partial" else "succeeded"
            sequence += 1
            await _send(
                writer,
                _event(
                    request_id,
                    self._event(
                        run_id,
                        sequence,
                        "run.completed",
                        {
                            "outcome": outcome,
                            "usage": {"provenance": "unknown"},
                            "message": "fixture completion",
                        },
                    ),
                ),
            )
            await _send(
                writer,
                _response(
                    request_id,
                    self._result(
                        run_id,
                        CompletionStatus.COMPLETED,
                        Outcome.PARTIAL if outcome == "partial" else Outcome.SUCCEEDED,
                        events_seen=sequence,
                        terminal_kind=EventKind.RUN_COMPLETED,
                        terminal_sequence=sequence,
                    ),
                ),
            )
        finally:
            self._cancel.pop(run_id, None)

    # -------------------------------------------------------------- dispatch

    async def _cancel_run(
        self, request_id: str, params: dict[str, Any], writer: asyncio.StreamWriter
    ) -> None:
        run_id = str(params.get("run_id", ""))
        event = self._cancel.get(run_id)
        now = datetime.now(timezone.utc)
        if event is not None:
            event.set()
        await _send(
            writer,
            _response(
                request_id,
                CancelResult(
                    run_id=run_id or "unknown",
                    requested=True,
                    requested_at=now,
                    confirmed=event is not None,
                    confirmed_at=now if event is not None else None,
                    deadline_seconds=1.0,
                    detail=(
                        "fixture cancelled"
                        if event is not None
                        else "fixture has no such active run"
                    ),
                ).model_dump(mode="json"),
            ),
        )

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    await _send(
                        writer, _error("", "MALFORMED_REQUEST", "invalid frame")
                    )
                    return
                request_id = str(request.get("id", ""))
                method = request.get("method")
                params = request.get("params") or {}
                if method == "manifest":
                    await _send(writer, _response(request_id, MANIFEST))
                elif method == "probe":
                    await _send(writer, _response(request_id, PROBE))
                elif method == "discover_models":
                    await _send(writer, _response(request_id, MODELS))
                elif method == "runtime":
                    await _send(writer, _response(request_id, RUNTIME))
                elif method == "run":
                    await self._run(request_id, params, writer)
                elif method == "cancel":
                    await self._cancel_run(request_id, params, writer)
                elif method == "shutdown":
                    await _send(
                        writer,
                        _response(
                            request_id,
                            {"instance_id": self.instance_id, "stopping": True},
                        ),
                    )
                    return self.request_stop()
                else:
                    await _send(
                        writer,
                        _error(
                            request_id, "UNKNOWN_METHOD", f"unknown method {method!r}"
                        ),
                    )
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            return
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass

    def request_stop(self) -> None:
        if self._server is not None:
            self._server.close()

    async def serve(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle, path=self.socket_path
            )
        finally:
            os.umask(old_umask)
        async with self._server:
            await self._server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--instance-id", default="runner-1")
    args = parser.parse_args()
    os.makedirs(args.root, mode=0o700, exist_ok=True)
    asyncio.run(FixtureRunner(args.socket, args.root, args.instance_id).serve())


if __name__ == "__main__":
    main()
