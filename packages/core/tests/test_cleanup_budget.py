"""Real-UDS regression for the cancel-cleanup budget.

The Runner declares its bounded cancellation cleanup over the ``runtime`` RPC.
The API must derive its finite outer run budget from that validated value, not
from its own ``api.cancel_deadline_seconds`` (which operators may have set
differently). A mismatched API value must never cut the Runner's unwind short.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from cli_driver_mock import MockDriver
from cli_provider_core import RunController, RunnerRegistry, Store
from cli_provider_core.controller import _SCHEDULING_MARGIN_SECONDS
from cli_provider_runner import RunnerServer

from conftest import base_config


async def _start_runner(
    tmp_path, *, cancel_deadline: float
) -> tuple[RunnerServer, str, asyncio.Task]:
    socket_path = str(tmp_path / "cleanup.sock")
    server = RunnerServer(
        socket_path=socket_path,
        instance_id="runner-1",
        driver=MockDriver("success"),
        cancel_deadline_seconds=cancel_deadline,
    )
    server.load()
    serve_task = asyncio.create_task(server.serve())
    for _ in range(300):
        if os.path.exists(socket_path):
            break
        await asyncio.sleep(0.01)
    assert os.path.exists(socket_path), "runner socket never became ready"
    return server, socket_path, serve_task


@pytest.mark.asyncio
async def test_outer_budget_follows_runner_cleanup_over_mismatched_api_config(tmp_path):
    # The Runner's worst case is two bounded cancel waits (2 x 6 s = 12 s).
    server, socket_path, serve_task = await _start_runner(tmp_path, cancel_deadline=6.0)
    try:
        config = base_config(
            tmp_path,
            api={"cancel_deadline_seconds": 0.5},
            runners=[
                {
                    "instance_id": "runner-1",
                    "driver_id": "mock",
                    "driver_version": "0.1.0",
                    "distribution": "cli-driver-mock",
                    "socket_path": socket_path,
                }
            ],
        )
        store = Store(config.db_path())
        store.initialize()
        try:
            registry = RunnerRegistry(config)
            await registry.refresh()
            health = registry.runner_health("runner-1")
            assert health.ok is True, health.detail
            assert health.cancel_cleanup_seconds == 12.0
            assert registry.runner_cleanup_seconds("runner-1") == 12.0

            controller = RunController(config=config, store=store, registry=registry)
            # API cancel deadline is 0.5 s, yet the budget uses the Runner's 12 s.
            assert controller.run_budget("runner-1", 2.0) == (
                2.0 + 12.0 + _SCHEDULING_MARGIN_SECONDS
            )
        finally:
            store.close()
    finally:
        server.request_stop()
        await asyncio.wait_for(serve_task, timeout=5)
