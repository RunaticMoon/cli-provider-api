"""Single-API-owner enforcement: one live API process per canonical store path.

``Store`` documents "single API instance"; ``ownerlock`` enforces it at the
application boundary with a lifetime kernel lock. These tests run real API
subprocesses on disposable roots: a second owner is refused while the first is
serving (without reconciling its rows), and the lock releases cleanly for a
later restart with the unknown-attempt guard still holding.
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

from cli_provider_api.ownerlock import (
    ApiOwnerError,
    acquire_store_owner_lock,
    release_store_owner_lock,
)
from cli_provider_core import Store
from conftest import MockSystem
from test_lifecycle import _StreamThread, body

CHAT = "/v1/chat/completions"

_HANG_OVERRIDES = {
    "api": {
        "default_run_deadline_seconds": 30.0,
        "max_run_deadline_seconds": 60.0,
        "cancel_deadline_seconds": 2.0,
    }
}


def _wait_running(system: MockSystem, run_id: str) -> dict:
    with system.client() as client:
        deadline = time.time() + 15
        while time.time() < deadline:
            view = client.get(f"/api/v1/runs/{run_id}").json()
            if view["status"] in ("starting", "running"):
                return view
            time.sleep(0.05)
    raise AssertionError(f"run {run_id} never dispatched")


def test_owner_lock_rejects_second_holder_and_aliased_path(tmp_path):
    db = str(tmp_path / "data" / "core.db")
    fd = acquire_store_owner_lock(db)
    try:
        # An aliased spelling of the same canonical DB resolves to one lock.
        alias = str(tmp_path / "data" / ".." / "data" / "core.db")
        with pytest.raises(ApiOwnerError):
            acquire_store_owner_lock(alias)
        with pytest.raises(ApiOwnerError):
            acquire_store_owner_lock(db)
        # Plain Store connections are unaffected by the owner lock.
        store = Store(db)
        store.initialize(reconcile=False)
        assert store.get_meta("last_reconcile") is None
        store.close()
    finally:
        release_store_owner_lock(fd)
    # Release (never unlink) lets a later owner take the same lock.
    fd2 = acquire_store_owner_lock(db)
    release_store_owner_lock(fd2)
    assert os.path.exists(os.path.realpath(db) + ".api-owner.lock")


def test_owner_lock_refuses_a_symlinked_lock_file(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = str(data / "core.db")
    target = tmp_path / "planted"
    target.write_text("x", encoding="utf-8")
    os.symlink(target, os.path.realpath(db) + ".api-owner.lock")
    with pytest.raises(ApiOwnerError):
        acquire_store_owner_lock(db)


def test_second_api_process_is_refused_while_first_owns_store(system_factory):
    """A real second API on the same DB exits cleanly and never reconciles or
    disturbs the first owner's in-flight run."""
    system = system_factory("hang", config_overrides=_HANG_OVERRIDES)
    stream = _StreamThread(system, body(task="task-owned")).start()
    try:
        _wait_running(system, stream.run_id)

        proc, _port, log_path = system.spawn_api()
        assert proc.wait(timeout=30) != 0
        with open(log_path, encoding="utf-8") as handle:
            assert "already owns this store" in handle.read()

        with system.client() as client:
            # The refused process never reconciled: the first owner's row is
            # still in flight, not rewritten to unknown, and finishes unchanged.
            assert (
                client.get(f"/api/v1/runs/{stream.run_id}").json()["status"]
                in ("starting", "running")
            )
            cancel = client.post(f"/api/v1/runs/{stream.run_id}/cancel")
            assert cancel.json()["confirmed"] is True
            assert (
                client.get(f"/api/v1/runs/{stream.run_id}").json()["status"]
                == "cancelled"
            )
    finally:
        try:
            with system.client() as client:
                client.post(f"/api/v1/runs/{stream.run_id}/cancel")
        except Exception:  # noqa: BLE001 - cleanup best-effort
            pass
        stream.join(timeout=30)


def test_lock_release_allows_restart_and_unknown_guard_holds(system_factory):
    """After the first owner dies the kernel releases the lock: a fresh API
    starts on the same DB, reconciles the orphaned run to unknown, and the
    retry guard still refuses a resubmission."""
    system = system_factory("hang", config_overrides=_HANG_OVERRIDES)
    stream = _StreamThread(system, body(task="task-unk2")).start()
    _wait_running(system, stream.run_id)
    system.kill_api()  # unclean stop; the kernel releases the owner lock
    stream.join()  # the dropped HTTP connection ends the SSE read

    proc, port, _log_path = system.spawn_api()
    system.wait_api_ready(port, proc)

    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": f"Bearer {system.api_key}"},
        timeout=15.0,
    ) as client:
        view = client.get(f"/api/v1/runs/{stream.run_id}").json()
        assert view["status"] == "unknown"
        retry = client.post(CHAT, json=body(task="task-unk2"))
        assert retry.status_code == 409
        assert retry.json()["error"]["code"] in {
            "unknown_attempt",
            "run_active",
            "run_not_retryable",
        }
