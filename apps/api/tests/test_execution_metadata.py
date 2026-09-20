"""Authenticated execution metadata contract (metadata.execution).

Exact shape (single nested object, complete-if-present, bounded scalars):

    "execution": {
        "task_revision":  "<id>",
        "base_revision":  "<id>",
        "route":          "role.capability.tier",
        "policy_version": "<id>",
    }

Identity remains the authenticated principal plus task_id/workspace_id;
run_id/attempt_id are wrapper-generated and canonical — a request can never
supply or overwrite them. The context participates in the request hash, so a
replay under the same task id with mutated context is a content conflict.
"""

from __future__ import annotations

import pytest

from conftest import MockSystem

CHAT = "/v1/chat/completions"

EXECUTION = {
    "task_revision": "rev-7",
    "base_revision": "base-2026.09",
    "route": "worker.code.standard",
    "policy_version": "pol-3",
}


def body(task="task-1", execution="sentinel", **extra):
    metadata = {"task_id": task, "workspace_id": "ws-alpha"}
    if execution != "sentinel":
        metadata["execution"] = execution
    return {
        "model": "mock/text",
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": metadata,
        **extra,
    }


def test_execution_context_is_persisted_and_echoed(system: MockSystem):
    """A stock Runner rejects the unknown `execution` run param pre-execution
    (INVALID_PARAMS) — the context still reaches the durable attempt record and
    the normalized run view, and the failure is a proven pre-execution
    rejection, never a retried execution."""
    with system.client() as client:
        response = client.post(CHAT, json=body(execution=EXECUTION))
        assert response.status_code == 502
        payload = response.json()
        assert payload["error"]["code"] == "run_rejected"
        run = payload["run"]
        assert run["status"] == "failed" and run["outcome"] == "rejected"
        assert run["execution"] == EXECUTION
        # Readback through the management route returns the same context.
        again = client.get(f"/api/v1/runs/{run['run_id']}").json()
        assert again["execution"] == EXECUTION

        # Same task + same context replays the rejection admission path: the
        # new attempt is admitted (proven pre-execution) and rejected again.
        retry = client.post(CHAT, json=body(execution=EXECUTION))
        assert retry.status_code == 502
        assert retry.json()["run"]["run_id"] != run["run_id"]


def test_execution_context_binds_into_request_hash(system: MockSystem):
    with system.client() as client:
        assert client.post(CHAT, json=body(execution=EXECUTION)).status_code == 502
        # Same task id with mutated context is a content conflict.
        mutated = dict(EXECUTION, task_revision="rev-8")
        conflict = client.post(CHAT, json=body(execution=mutated))
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "task_content_conflict"
        # Dropping the context is likewise a different logical request.
        dropped = client.post(CHAT, json=body())
        assert dropped.status_code == 409
        assert dropped.json()["error"]["code"] == "task_content_conflict"


@pytest.mark.parametrize(
    "execution",
    [
        "worker.code.standard",  # not an object
        {"task_revision": "r1", "base_revision": "b1", "route": "worker.code.standard"},
        dict(EXECUTION, unknown_field="x"),  # extra key
        dict(EXECUTION, run_id="run_evil"),  # canonical id overwrite attempt
        dict(EXECUTION, task_revision="../escape"),  # path-shaped value
        dict(EXECUTION, base_revision="/etc/passwd"),
        dict(EXECUTION, route="worker.code"),  # route must be 3 segments
        dict(EXECUTION, route="worker/code/standard"),  # slashes rejected
        dict(EXECUTION, route="a.b.c.d"),  # too many segments
        dict(EXECUTION, policy_version="x" * 200),  # unbounded scalar
        dict(EXECUTION, task_revision=42),  # non-string
    ],
)
def test_invalid_execution_context_rejected_before_execution(system: MockSystem, execution):
    with system.client() as client:
        response = client.post(CHAT, json=body(execution=execution))
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request_error"
        assert "X-Run-Id" not in response.headers
        # The task id was never reserved: a clean request still works.
        followup = client.post(CHAT, json=body(task="task-1"))
        assert followup.status_code == 200


def test_metadata_run_id_and_attempt_id_are_not_accepted(system: MockSystem):
    with system.client() as client:
        for key in ("run_id", "attempt_id"):
            payload = body()
            payload["metadata"][key] = "attacker-supplied"
            response = client.post(CHAT, json=payload)
            assert response.status_code == 400
