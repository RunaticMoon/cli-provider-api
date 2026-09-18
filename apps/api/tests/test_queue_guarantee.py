"""Real UDS guarantee: a run queued behind >5 s of work is not a false `unknown`.

Uses the real API + real Runner subprocess + real Unix socket with the mock
driver's operator-selected ``slow`` behaviour (~6 s). This exercises the queue
window that the former outer budget did not cover.
"""

from __future__ import annotations

import time

import httpx

from conftest import MockSystem
from test_lifecycle import _StreamThread, body

CHAT = "/v1/chat/completions"

SLOW_OVERRIDES = {
    "api": {
        "default_run_deadline_seconds": 30.0,
        "max_run_deadline_seconds": 60.0,
        "cancel_deadline_seconds": 2.0,
        "keepalive_seconds": 0.3,
        "concurrency": {
            "per_runner": 1,
            "per_principal": 2,
            "queue_timeout_seconds": 15.0,
        },
    }
}


def test_queued_run_behind_over_5s_work_is_not_false_unknown(system_factory):
    system: MockSystem = system_factory("slow", config_overrides=SLOW_OVERRIDES)
    first = _StreamThread(system, body(task="slow-1")).start()
    try:
        with system.client() as client:
            deadline = time.time() + 15
            status = None
            while time.time() < deadline:
                status = client.get(f"/api/v1/runs/{first.run_id}").json()["status"]
                if status == "running":
                    break
                time.sleep(0.05)
            assert status == "running"

            # The single verified Runner slot is occupied by the slow first run.
            # The second request is admitted and waits in the API/core queue; a
            # 200 here also proves the Runner was never quarantined.
            started = time.monotonic()
            with httpx.Client(
                base_url=system.base_url,
                headers={"Authorization": f"Bearer {system.api_key}"},
                timeout=30.0,
            ) as long_client:
                second = long_client.post(
                    CHAT, json=body(task="slow-2", content="second")
                )
            elapsed = time.monotonic() - started
    finally:
        first.join()

    assert first.error is None
    assert second.status_code == 200, second.text
    second_run = second.json()["run"]
    assert second_run["status"] == "completed"
    assert second_run["outcome"] == "succeeded"
    # The first run held the slot for >5 s, so the second was genuinely queued.
    assert elapsed > 5.0

    with system.client() as client:
        first_run = client.get(f"/api/v1/runs/{first.run_id}").json()
    assert first_run["status"] == "completed"
