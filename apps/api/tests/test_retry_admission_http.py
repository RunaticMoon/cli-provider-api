"""HTTP-level retry-safety contract.

The durable cross-candidate admission guard lives in the shared Store (see
packages/core/tests/test_retry_safety.py); these tests pin the HTTP surface a
sequential-fallback gateway actually observes: an attempt that may have had
Runner-side effects is never silently retried under the same or a different
model — the follow-up request is an explicit ``run_not_retryable`` conflict with
the original run identity preserved for control readback.
"""

from __future__ import annotations

import time

from conftest import MockSystem
from test_lifecycle import _StreamThread, body

CHAT = "/v1/chat/completions"


def test_failed_run_is_never_retried_same_or_other_model(run_failure_system: MockSystem):
    with run_failure_system.client(key=run_failure_system.gamma_key) as client:
        first = client.post(CHAT, json=body(task="task-f"))
        assert first.status_code == 502
        assert first.json()["run"]["status"] == "failed"
        run_id = first.json()["run"]["run_id"]

        for model in ("mock/text", "mock/text-beta"):
            retry = client.post(CHAT, json=body(task="task-f", model=model))
            assert retry.status_code == 409, retry.text
            error = retry.json()["error"]
            assert error["code"] == "run_not_retryable"
            # The original run identity is preserved for control-plane readback.
            assert error["run_id"] == run_id
            assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "failed"


def test_dispatched_cancel_is_never_retried(long_hang_system: MockSystem):
    stream = _StreamThread(long_hang_system, body(task="task-cx")).start()
    try:
        with long_hang_system.client() as client:
            cancel = client.post(f"/api/v1/runs/{stream.run_id}/cancel")
            assert cancel.json()["confirmed"] is True
            assert (
                client.get(f"/api/v1/runs/{stream.run_id}").json()["status"]
                == "cancelled"
            )
            retry = client.post(CHAT, json=body(task="task-cx", model="mock/text"))
            assert retry.status_code == 409
            assert retry.json()["error"]["code"] == "run_not_retryable"
    finally:
        stream.join()


def test_cancelled_while_queued_may_be_explicitly_retried(queue_system: MockSystem):
    """Positive control: a queued cancel never dispatched, so resubmit is legal."""
    first = _StreamThread(queue_system, body(task="task-hold")).start()
    second = _StreamThread(queue_system, body(task="task-qc")).start()
    try:
        with queue_system.client() as client:
            assert client.post(f"/api/v1/runs/{second.run_id}/cancel").json()[
                "confirmed"
            ] is True
            assert (
                client.get(f"/api/v1/runs/{second.run_id}").json()["status"]
                == "cancelled"
            )
            client.post(f"/api/v1/runs/{first.run_id}/cancel")
            deadline = time.time() + 10
            while time.time() < deadline:
                if (
                    client.get(f"/api/v1/runs/{first.run_id}").json()["status"]
                    == "cancelled"
                ):
                    break
                time.sleep(0.05)

            retry = client.post(CHAT, json=body(task="task-qc"))
            assert retry.status_code != 409 or (
                retry.json()["error"]["code"] != "run_not_retryable"
            )
            new_run = retry.json()["run"]
            assert new_run["run_id"] != second.run_id
            client.post(f"/api/v1/runs/{new_run['run_id']}/cancel")
    finally:
        first.join()
        second.join()


def test_partial_completion_replays_cached_never_reexecutes(system_factory):
    system = system_factory("partial")
    with system.client() as client:
        first = client.post(CHAT, json=body(task="task-part"))
        assert first.status_code == 200
        assert first.json()["run"]["outcome"] == "partial"
        replay = client.post(CHAT, json=body(task="task-part"))
        assert replay.status_code == 200
        assert replay.json()["run"]["cached"] is True
        assert replay.json()["run"]["run_id"] == first.json()["run"]["run_id"]


def test_unknown_outcome_stays_blocked_after_restart(system_factory):
    system = system_factory(
        "hang_ignores_cancel",
        config_overrides={
            "api": {
                "default_run_deadline_seconds": 0.3,
                "max_run_deadline_seconds": 0.5,
                "cancel_deadline_seconds": 0.5,
            }
        },
    )
    with system.client() as client:
        first = client.post(CHAT, json=body(task="task-unk"))
        assert first.status_code == 502
        assert first.json()["run"]["status"] == "unknown"

    system.restart_api()

    with system.client() as client:
        retry = client.post(CHAT, json=body(task="task-unk"))
        assert retry.status_code == 409
        assert retry.json()["error"]["code"] in {"unknown_attempt", "run_active"}
