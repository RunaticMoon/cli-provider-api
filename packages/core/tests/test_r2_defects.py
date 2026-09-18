"""Behavioral regression tests for the M1 release-review r2 findings.

Four bounded truthfulness/config defects:
  1. a post-terminal artifact-persistence failure must not downgrade a validated
     completed terminal to `unknown` or quarantine the Runner;
  2. the global `api.concurrency.per_principal` must actually bound admission
     (min with the principal's own `max_concurrency`);
  3. the synthetic `allow_synthetic_unverified` opt-in may cover only
     unknown/not_run, never an explicit `failed` verification;
  4. `synthetic` provenance must be derived from the verified Runner manifest
     and persisted per attempt (with a migration for an existing M1 DB).
"""

from __future__ import annotations

import asyncio
import errno
import os
import sqlite3

import pytest

from cli_provider_core import QueueFull, RunnerRegistry, Store, hash_api_key
from cli_provider_core.models import COMPLETED, OUTCOME_PARTIAL, OUTCOME_SUCCEEDED
from conftest import FakeControl, FakeSession, base_config, make_system
from test_controller import prepared, submit
from test_registry import _model, make_registry


# --------------------------------------------------------------------- item 1


def _patch_artifact_persistence(monkeypatch, controller, store, failure: str) -> None:
    if failure == "store":

        def boom(record) -> None:
            raise sqlite3.OperationalError("artifact store write failed")

        monkeypatch.setattr(store, "add_artifact", boom)
        return

    errno_value = errno.ENOSPC if failure == "enospc" else errno.EACCES
    real_open = os.open

    def guarded_open(path, flags, mode=0o777):
        if str(path).startswith(controller._config.artifacts_dir()):
            raise OSError(errno_value, os.strerror(errno_value), str(path))
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", guarded_open)


@pytest.mark.parametrize("failure", ["enospc", "eacces", "store"])
async def test_completed_terminal_survives_artifact_persistence_failure(
    tmp_path, monkeypatch, failure
):
    config, store, registry, controller, control = await prepared(tmp_path)
    _patch_artifact_persistence(monkeypatch, controller, store, failure)

    sub = await submit(controller, registry)
    record = await sub.active.task

    # The validated completed terminal is authoritative and is never overwritten.
    assert record.status == COMPLETED
    assert record.outcome == OUTCOME_SUCCEEDED
    assert record.summary == "hello world"
    assert record.verification["status"] == "not_run"
    # The persistence failure is recorded safely and never quarantines.
    assert "artifact" in (record.detail or "").lower()
    assert "enospc" not in (record.detail or "").lower()
    assert str(tmp_path) not in (record.detail or "")
    assert store.get_quarantine("runner-1") is None
    # Nothing claims the artifact exists.
    assert store.list_artifacts(record.run_id) == []
    store.close()


async def test_partial_completion_survives_artifact_persistence_failure(
    tmp_path, monkeypatch
):
    config, store, registry, controller, control = await prepared(tmp_path)
    control.behavior = "partial"
    _patch_artifact_persistence(monkeypatch, controller, store, "store")

    sub = await submit(controller, registry)
    record = await sub.active.task

    assert record.status == COMPLETED
    assert record.outcome == OUTCOME_PARTIAL
    assert store.get_quarantine("runner-1") is None
    assert store.list_artifacts(record.run_id) == []
    store.close()


async def test_successful_artifact_path_still_persists(tmp_path):
    """Positive control: the guarded path does not swallow a normal write."""
    config, store, registry, controller, control = await prepared(tmp_path)
    sub = await submit(controller, registry)
    record = await sub.active.task
    assert record.status == COMPLETED
    assert len(store.list_artifacts(record.run_id)) == 1
    store.close()


# --------------------------------------------------------------------- item 2


def _principals(*specs: tuple[str, int]) -> list[dict]:
    return [
        {
            "name": name,
            "key_hash": hash_api_key(f"secret-{name}"),
            "allowed_presets": ["mock/text", "mock/text-beta", "mock/review"],
            "allowed_workspaces": ["ws-alpha"],
            "max_concurrency": limit,
        }
        for name, limit in specs
    ]


def _concurrency(**overrides) -> dict:
    values = {
        "per_runner": 8,
        "per_principal": 1,
        "max_queued_per_principal": 2,
        "queue_timeout_seconds": 30.0,
    }
    values.update(overrides)
    return values


async def test_global_per_principal_bounds_admission_below_principal_max(tmp_path):
    """The global per_principal is enforced as min(global, principal.max_concurrency)."""
    config, store, registry, controller, control = make_system(
        tmp_path,
        control=FakeControl(behavior="hang"),
        principals=_principals(("alpha", 5), ("beta", 5)),
        api={"concurrency": _concurrency()},
    )
    await registry.refresh()

    a1 = await submit(controller, registry, task="a-1", principal="alpha")
    a2 = await submit(controller, registry, task="a-2", principal="alpha")
    a3 = await submit(controller, registry, task="a-3", principal="alpha")
    await asyncio.sleep(0.1)
    # The global cap of 1 is binding even though alpha declares 5.
    assert control.executions == 1
    # Admission bound = per_principal + max_queued_per_principal = 3 outstanding.
    with pytest.raises(QueueFull):
        await submit(controller, registry, task="a-4", principal="alpha")

    # A saturated principal never consumes another principal's capacity.
    b1 = await submit(controller, registry, task="b-1", principal="beta")
    b2 = await submit(controller, registry, task="b-2", principal="beta")
    b3 = await submit(controller, registry, task="b-3", principal="beta")
    with pytest.raises(QueueFull):
        await submit(controller, registry, task="b-4", principal="beta")

    for sub in (a1, a2, a3, b1, b2, b3):
        await controller.cancel(sub.record.run_id, sub.record.principal)
    store.close()


async def test_effective_principal_uses_principal_lower_cap(tmp_path):
    """min() still honors a principal *lower* than the global cap."""
    config, store, registry, controller, control = make_system(
        tmp_path,
        control=FakeControl(behavior="hang"),
        principals=_principals(("alpha", 1)),
        api={"concurrency": _concurrency(per_principal=5, max_queued_per_principal=1)},
    )
    await registry.refresh()

    a1 = await submit(
        controller, registry, task="a-1", principal="alpha", principal_concurrency=1
    )
    a2 = await submit(
        controller, registry, task="a-2", principal="alpha", principal_concurrency=1
    )
    with pytest.raises(QueueFull):
        await submit(
            controller, registry, task="a-3", principal="alpha", principal_concurrency=1
        )
    for sub in (a1, a2):
        await controller.cancel(sub.record.run_id, sub.record.principal)
    store.close()


# --------------------------------------------------------------------- item 3


def _presets(opt_in: bool) -> list[dict]:
    return [
        {
            "alias": alias,
            "runner_ref": "runner-1",
            "model_id": "mock-model",
            "allow_synthetic_unverified": opt_in,
            **({"task_policy": "review"} if alias == "mock/review" else {}),
        }
        for alias in ("mock/text", "mock/text-beta", "mock/review")
    ]


@pytest.mark.parametrize("opt_in", [True, False])
async def test_synthetic_failed_is_unavailable_even_with_opt_in(tmp_path, opt_in):
    registry = make_registry(
        tmp_path, {"models": [_model("failed")]}, presets=_presets(opt_in)
    )
    await registry.refresh()
    health = registry.preset_health("mock/text")
    assert health.verified is False
    assert health.real_verification is False
    assert registry.preset_available("mock/text") is False
    assert "failed" in health.detail


@pytest.mark.parametrize("status", ["unknown", "not_run"])
async def test_synthetic_unknown_and_not_run_stay_opt_in_servable(tmp_path, status):
    registry = make_registry(
        tmp_path, {"models": [_model(status)]}, presets=_presets(True)
    )
    await registry.refresh()
    health = registry.preset_health("mock/text")
    assert health.verified is True
    assert health.real_verification is False
    assert registry.preset_available("mock/text") is True


# --------------------------------------------------------------------- item 4


class NativeFakeSession(FakeSession):
    """A verified, non-synthetic Runner double (the opposite of the mock)."""

    async def manifest(self) -> dict:
        manifest = await super().manifest()
        manifest["synthetic"] = False
        return manifest

    async def discover_models(self) -> list[dict]:
        return [
            {
                "model_id": "mock-model",
                "display_name": "Native Model",
                "verification": {"status": "passed", "source": "fixture"},
            }
        ]


async def test_attempt_persists_runner_synthetic_provenance(tmp_path):
    config, store, registry, controller, control = make_system(
        tmp_path, control=FakeControl()
    )
    registry._session_factory = lambda cfg: NativeFakeSession(cfg, control)
    await registry.refresh()
    assert registry.runner_synthetic("runner-1") is False
    assert registry.preset_health("mock/text").real_verification is True

    sub = await submit(controller, registry)
    record = await sub.active.task
    assert record.synthetic is False
    # Persisted, not just in-memory: a fresh read of the store agrees.
    assert store.get_attempt(record.run_id).synthetic is False
    store.close()


async def test_mock_attempt_persists_synthetic_true(tmp_path):
    config, store, registry, controller, control = await prepared(tmp_path)
    assert registry.runner_synthetic("runner-1") is True
    sub = await submit(controller, registry)
    record = await sub.active.task
    assert record.synthetic is True
    store.close()


async def test_unverified_runner_defaults_synthetic_not_real(tmp_path):
    registry = RunnerRegistry(base_config(tmp_path))
    # Nothing probed yet: never claim a real (non-synthetic) run.
    assert registry.runner_synthetic("runner-1") is True
    assert registry.runner_synthetic("missing-runner") is True


_OLD_ATTEMPTS_SCHEMA = """
CREATE TABLE attempts (
    run_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE,
    preset TEXT NOT NULL,
    driver_id TEXT NOT NULL,
    runner_instance TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    summary TEXT,
    detail TEXT,
    verification TEXT,
    usage TEXT,
    cached INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
"""


def test_old_m1_db_without_synthetic_column_migrates(tmp_path):
    db_path = str(tmp_path / "core.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_OLD_ATTEMPTS_SCHEMA)
    conn.execute(
        "INSERT INTO attempts(run_id, principal, task_id, attempt_id, preset, "
        "driver_id, runner_instance, workspace_id, request_hash, status, cached, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run_old", "alpha", "task-1", "att_old", "mock/text", "mock",
            "runner-1", "ws-alpha", "sha256:a", "completed", 0, "t", "t",
        ),
    )
    conn.commit()
    conn.close()

    store = Store(db_path)
    store.initialize()
    columns = {row[1] for row in store._conn.execute("PRAGMA table_info(attempts)")}
    assert "synthetic" in columns
    # Existing M1 rows were synthetic; the migration must not claim otherwise.
    assert store.get_attempt("run_old").synthetic is True
    store.close()


def test_fresh_db_has_synthetic_column_and_reserve_persists_it(tmp_path):
    store = Store(str(tmp_path / "fresh.db"))
    store.initialize()
    columns = {row[1] for row in store._conn.execute("PRAGMA table_info(attempts)")}
    assert "synthetic" in columns
    record = store.reserve(
        run_id="run_new",
        attempt_id="att_new",
        principal="alpha",
        task_id="task-1",
        preset="mock/text",
        driver_id="mock",
        runner_instance="runner-1",
        workspace_id="ws-alpha",
        request_hash="sha256:a",
        task_policy="text",
        status="queued",
        synthetic=False,
    )
    assert record.synthetic is False
    assert store.get_attempt("run_new").synthetic is False
    store.close()
