"""Shared fixtures: config builder, fake Runner sessions, in-memory system."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import pytest

from cli_provider_core import (
    OperatorConfig,
    RunnerRegistry,
    RunController,
    Store,
    hash_api_key,
)
from cli_provider_sdk import (
    SDK_VERSION,
    CancelResult,
    CompletionStatus,
    EventKind,
    Outcome,
    RunResult,
    Usage,
    UsageProvenance,
    Verification,
    VerificationStatus,
)


def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def base_config(tmp_path, **overrides) -> OperatorConfig:
    data: dict[str, Any] = {
        "schema_version": 1,
        "data_dir": str(tmp_path / "data"),
        "api": {
            "default_run_deadline_seconds": 5.0,
            "max_run_deadline_seconds": 10.0,
            "cancel_deadline_seconds": 1.0,
            "keepalive_seconds": 0.5,
            "limits": {"max_body_bytes": 65536},
            "concurrency": {"per_runner": 2, "per_principal": 2, "queue_timeout_seconds": 1.0},
        },
        "runners": [
            {
                "instance_id": "runner-1",
                "driver_id": "mock",
                "driver_version": "0.1.0",
                "distribution": "cli-driver-mock",
                "socket_path": str(tmp_path / "runner.sock"),
            }
        ],
        "presets": [
            {
                "alias": "mock/text",
                "runner_ref": "runner-1",
                "model_id": "mock-model",
                "allow_synthetic_unverified": True,
            },
            {
                "alias": "mock/text-beta",
                "runner_ref": "runner-1",
                "model_id": "mock-model",
                "allow_synthetic_unverified": True,
            },
            {
                "alias": "mock/review",
                "runner_ref": "runner-1",
                "model_id": "mock-model",
                "task_policy": "review",
                "allow_synthetic_unverified": True,
            },
        ],
        "workspaces": [{"workspace_id": "ws-alpha"}],
        "principals": [
            {
                "name": "alpha",
                "key_hash": hash_api_key("secret-alpha"),
                "allowed_presets": ["mock/text", "mock/text-beta", "mock/review"],
                "allowed_workspaces": ["ws-alpha"],
                "max_concurrency": 2,
            }
        ],
    }
    _merge(data, overrides)
    return OperatorConfig.model_validate(data)


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


@dataclass
class FakeControl:
    behavior: str = "success"
    executions: int = 0
    cancel_calls: int = 0
    cancel_confirmed: bool = True
    hang_event: asyncio.Event = field(default_factory=asyncio.Event)
    delay_seconds: float = 0.0
    event_count: int = 300
    run_timeouts: list[float] = field(default_factory=list)
    # The fake Runner does not serialise, so it explicitly declares a capacity
    # that matches the fixture's configured (multi-runner) concurrency.
    max_parallel_runs: int = 8
    max_queue: int = 8
    # Runner-owned cancellation cleanup budget, declared over the runtime RPC.
    cancel_cleanup_seconds: float = 2.0
    # Catalog fixture control: descriptor dicts the fake reports, plus a call
    # counter so tests can prove refresh is singleflight/TTL-bounded.
    models: list[dict[str, Any]] | None = None
    discovery_calls: int = 0
    discovery_fail: bool = False
    # Simulated slow verification for singleflight-under-slow-pass tests.
    discovery_delay_seconds: float = 0.0


class FakeSession:
    """A RunnerSession double with deterministic, event-driven behaviour."""

    def __init__(self, config, control: FakeControl) -> None:
        self.config = config
        self.control = control
        self.last_result: RunResult | None = None
        # Mirror UdsRunnerSession: the operator override is an attribute the
        # controller reads and bounds.
        self.run_timeout_seconds = config.run_frame_timeout_seconds

    async def manifest(self) -> dict[str, Any]:
        return {
            "driver_id": self.config.driver_id,
            "name": "Mock",
            "version": self.config.driver_version,
            "sdk_version": SDK_VERSION,
            "protocol_family": "mock",
            "supported_transports": ["stdio"],
            "synthetic": True,
        }

    async def probe(self) -> dict[str, Any]:
        return {
            "ok": True,
            "driver_id": self.config.driver_id,
            "driver_version": self.config.driver_version,
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
            "notes": ["synthetic test double"],
        }

    async def discover_models(self) -> list[dict[str, Any]]:
        self.control.discovery_calls += 1
        if self.control.discovery_delay_seconds:
            await asyncio.sleep(self.control.discovery_delay_seconds)
        if self.control.discovery_fail:
            raise RuntimeError("fake catalog read failed")
        if self.control.models is not None:
            return self.control.models
        return [
            {
                "model_id": "mock-model",
                "display_name": "Mock Model",
                "verification": {
                    "status": "unknown",
                    "source": "fake-fixture",
                    "reason": "test double; not really verified",
                },
            }
        ]

    async def runtime(self) -> dict[str, Any]:
        return {
            "max_parallel_runs": self.control.max_parallel_runs,
            "max_queue": self.control.max_queue,
            "cancel_cleanup_seconds": self.control.cancel_cleanup_seconds,
        }

    def _event(self, run_id: str, sequence: int, kind: str, payload: dict) -> dict:
        return {
            "schema_version": 1,
            "run_id": run_id,
            "sequence": sequence,
            "timestamp": ts(),
            "synthetic": True,
            "kind": kind,
            "payload": payload,
        }

    def _result(self, run_id: str, status, outcome, sequence: int, events: int) -> RunResult:
        return RunResult(
            run_id=run_id,
            status=status,
            outcome=outcome,
            verification=Verification(
                status=VerificationStatus.NOT_RUN, source="fake"
            ),
            usage=Usage(provenance=UsageProvenance.UNKNOWN),
            terminal_kind=EventKind.RUN_COMPLETED
            if status is CompletionStatus.COMPLETED
            else (
                EventKind.RUN_FAILED
                if status is CompletionStatus.FAILED
                else EventKind.RUN_CANCELLED
            ),
            terminal_sequence=sequence,
            events_seen=events,
            synthetic=True,
        )

    async def run(self, params) -> AsyncIterator[dict[str, Any]]:
        run_id = params["run_id"]
        self.control.executions += 1
        behavior = self.control.behavior
        if behavior == "crash":
            raise RuntimeError("fake driver crash")
        if behavior == "many_events":
            count = self.control.event_count
            yield self._event(run_id, 1, "run.started", {"preset": params["preset"]})
            for index in range(count):
                yield self._event(
                    run_id, index + 2, "message.delta", {"text": f"d{index} "}
                )
            yield self._event(
                run_id,
                count + 2,
                "run.completed",
                {"outcome": "succeeded", "usage": {"provenance": "unknown"}},
            )
            self.last_result = self._result(
                run_id, CompletionStatus.COMPLETED, Outcome.SUCCEEDED, count + 2, count + 2
            )
            return
        if behavior == "hang":
            yield self._event(run_id, 1, "run.started", {"preset": params["preset"]})
            await self.control.hang_event.wait()
            yield self._event(run_id, 2, "run.cancelled", {"reason": "cancelled"})
            self.last_result = RunResult(
                run_id=run_id,
                status=CompletionStatus.CANCELLED,
                outcome=Outcome.CANCELLED,
                verification=Verification(
                    status=VerificationStatus.NOT_RUN, source="fake"
                ),
                usage=Usage(provenance=UsageProvenance.UNKNOWN),
                terminal_kind=EventKind.RUN_CANCELLED,
                terminal_sequence=2,
                events_seen=2,
                synthetic=True,
            )
            return

        if behavior == "release_completed":
            # Completes only once cancel() releases it, reproducing the race
            # where a run finishes while the cancel path is awaiting.
            yield self._event(run_id, 1, "run.started", {"preset": params["preset"]})
            await self.control.hang_event.wait()
            yield self._event(run_id, 2, "message.delta", {"text": "finished "})
            yield self._event(run_id, 3, "message.delta", {"text": "anyway"})
            yield self._event(
                run_id,
                4,
                "run.completed",
                {"outcome": "succeeded", "usage": {"provenance": "unknown"}},
            )
            self.last_result = self._result(
                run_id, CompletionStatus.COMPLETED, Outcome.SUCCEEDED, 4, 4
            )
            return

        if self.control.delay_seconds:
            await asyncio.sleep(self.control.delay_seconds)
        yield self._event(run_id, 1, "run.started", {"preset": params["preset"]})
        yield self._event(run_id, 2, "message.delta", {"text": "hello "})
        yield self._event(run_id, 3, "message.delta", {"text": "world"})
        if behavior == "failed":
            yield self._event(run_id, 4, "run.failed", {"code": "x", "message": "boom"})
            self.last_result = self._result(
                run_id, CompletionStatus.FAILED, Outcome.PROVIDER_ERROR, 4, 4
            )
            return
        if behavior == "unknown":
            self.last_result = RunResult(
                run_id=run_id,
                status=CompletionStatus.UNKNOWN,
                outcome=Outcome.UNKNOWN,
                verification=Verification(
                    status=VerificationStatus.UNKNOWN, source="fake", reason="abnormal"
                ),
                usage=Usage(provenance=UsageProvenance.UNKNOWN),
                terminal_kind=None,
                terminal_sequence=None,
                events_seen=3,
                synthetic=True,
            )
            return
        outcome = "partial" if behavior == "partial" else "succeeded"
        yield self._event(
            run_id, 4, "run.completed", {"outcome": outcome, "usage": {"provenance": "unknown"}}
        )
        self.last_result = RunResult(
            run_id=run_id,
            status=CompletionStatus.COMPLETED,
            outcome=Outcome(outcome),
            verification=Verification(
                status=VerificationStatus.NOT_RUN, source="fake"
            ),
            usage=Usage(provenance=UsageProvenance.UNKNOWN),
            terminal_kind=EventKind.RUN_COMPLETED,
            terminal_sequence=4,
            events_seen=4,
            synthetic=True,
        )

    def set_run_timeout(self, seconds: float) -> None:
        self.control.run_timeouts.append(seconds)

    async def cancel(self, run_id: str) -> CancelResult:
        self.control.cancel_calls += 1
        confirmed = self.control.cancel_confirmed
        if confirmed:
            self.control.hang_event.set()
        now = datetime.now(timezone.utc)
        return CancelResult(
            run_id=run_id,
            requested=True,
            requested_at=now,
            confirmed=confirmed,
            confirmed_at=now if confirmed else None,
            deadline_seconds=1.0,
            detail="fake cancel",
        )

    async def aclose(self) -> None:
        return None


def make_system(tmp_path, *, control: FakeControl | None = None, **overrides):
    control = control or FakeControl()
    config = base_config(tmp_path, **overrides)
    store = Store(config.db_path())
    store.initialize()
    registry = RunnerRegistry(
        config, session_factory=lambda cfg: FakeSession(cfg, control)
    )
    controller = RunController(config=config, store=store, registry=registry)
    return config, store, registry, controller, control


@pytest.fixture
def system(tmp_path):
    config, store, registry, controller, control = make_system(tmp_path)
    yield config, store, registry, controller, control
    store.close()
