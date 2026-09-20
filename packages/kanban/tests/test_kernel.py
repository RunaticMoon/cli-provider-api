"""Kernel bridge tests — real installed-Hermes interpreter, real temp
boards. Covers the lock (native executor collision), lifecycle ops, and
process cleanup."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from cli_provider_kanban.kernel import KernelBridge, KernelError

from conftest import requires_hermes

pytestmark = requires_hermes

HERMES_ENV_KEYS = ("HERMES_HOME", "HERMES_KANBAN_DB", "HERMES_KANBAN_HOME",
                   "HERMES_KANBAN_BOARD")


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    for key in HERMES_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    b = KernelBridge(tmp_path / "kanban.db",
                     env_extra={"HERMES_HOME": str(tmp_path / "hh")})
    yield b
    b.close()


def test_ping_and_close(bridge):
    out = bridge.call("ping")
    assert out["pid"]
    bridge.close()
    assert bridge._proc is None or bridge._proc.poll() is not None


def test_lock_excludes_second_bridge(bridge, tmp_path):
    """Two dispatchers must not tick the same board — the kernel-held lock
    serializes them (native executor collision)."""
    assert bridge.call("acquire_lock")["held"] is True
    b2 = KernelBridge(bridge.board_db,
                      env_extra={"HERMES_HOME": str(tmp_path / "hh")})
    try:
        assert b2.call("acquire_lock")["held"] is False
    finally:
        b2.close()
    bridge.call("release_lock")
    b3 = KernelBridge(bridge.board_db,
                      env_extra={"HERMES_HOME": str(tmp_path / "hh")})
    try:
        assert b3.call("acquire_lock")["held"] is True
        b3.call("release_lock")
    finally:
        b3.close()


def test_full_card_lifecycle(bridge):
    tid = bridge.call("create_task", title="card", assignee="jev-native",
                      body="spec")["task_id"]
    task = bridge.call("get_task", task_id=tid)["task"]
    assert task["status"] == "ready"

    claim = bridge.call("claim", task_id=tid, claimer="jev:test")
    assert claim["claimed"] is True
    assert claim["task"]["status"] == "running"
    assert bridge.call("heartbeat", task_id=tid, claimer="jev:test")["alive"]

    ok = bridge.call("request_review", task_id=tid, summary="done-ish",
                     expected_run_id=claim["task"]["current_run_id"])
    assert ok["ok"]
    assert bridge.call("get_task", task_id=tid)["task"]["status"] == "review"


def test_block_and_unblock(bridge):
    tid = bridge.call("create_task", title="card", assignee="jev-native",
                      body="x")["task_id"]
    bridge.call("block", task_id=tid, kind="needs_input", reason="waiting")
    assert bridge.call("get_task", task_id=tid)["task"]["status"] == "blocked"
    bridge.call("unblock", task_id=tid)
    assert bridge.call("get_task", task_id=tid)["task"]["status"] == "ready"


def test_unknown_op_raises(bridge):
    with pytest.raises(KernelError, match="unknown op"):
        bridge.call("definitely_not_an_op")


def test_bridge_process_terminates(bridge):
    pid = bridge.call("ping")["pid"]
    bridge.close()
    # The bridge child must be reaped — no orphan processes.
    try:
        os.kill(pid, 0)
        alive = True
    except (ProcessLookupError, PermissionError):
        alive = False
    assert not alive, f"bridge pid {pid} survived close()"


def test_bridge_error_includes_type(bridge):
    # Missing task is a clean None, not an exception.
    assert bridge.call("get_task", task_id="t_missing")["task"] is None
    # A malformed call raises a typed KernelError.
    with pytest.raises(KernelError):
        bridge.call("create_task")  # missing required title


def test_context_manager(tmp_path, monkeypatch):
    for key in HERMES_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    with KernelBridge(tmp_path / "k.db",
                      env_extra={"HERMES_HOME": str(tmp_path / "hh")}) as b:
        assert b.call("ping")["pid"]
    assert b._proc is None or b._proc.poll() is not None
