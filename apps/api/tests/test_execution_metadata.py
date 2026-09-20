"""Caller-supplied execution metadata contract (metadata.execution).

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


def body(task="task-1", execution="sentinel", workspace="ws-alpha", **extra):
    metadata = {"task_id": task, "workspace_id": workspace}
    if execution != "sentinel":
        metadata["execution"] = execution
    return {
        "model": "mock/text",
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": metadata,
        **extra,
    }


def test_execution_context_is_persisted_and_echoed(system: MockSystem):
    """The real Runner accepts typed context and completed retries reuse it."""
    with system.client() as client:
        response = client.post(CHAT, json=body(execution=EXECUTION))
        assert response.status_code == 200
        payload = response.json()
        run = payload["run"]
        assert run["status"] == "completed" and run["outcome"] == "succeeded"
        assert run["execution"] == EXECUTION
        # Readback through the management route returns the same context.
        again = client.get(f"/api/v1/runs/{run['run_id']}").json()
        assert again["execution"] == EXECUTION

        # Same task + same context reuses the durable completed attempt.
        retry = client.post(CHAT, json=body(execution=EXECUTION))
        assert retry.status_code == 200
        assert retry.json()["run"]["run_id"] == run["run_id"]
        assert retry.json()["run"]["cached"] is True


def test_execution_context_binds_into_request_hash(system: MockSystem):
    with system.client() as client:
        assert client.post(CHAT, json=body(execution=EXECUTION)).status_code == 200
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


def test_forged_wellformed_context_never_selects_preset_workspace_or_rights(
    system: MockSystem,
):
    """metadata.execution is caller-supplied evidence, never an authority.

    A well-formed but arbitrary route/policy is stored and echoed verbatim; it
    cannot change which preset/model/workspace the authorized request runs
    under, and it cannot widen the principal's rights.
    """
    forged = {
        "task_revision": "rev-999",
        "base_revision": "base-0",
        "route": "lead.plan.premium",
        "policy_version": "pol-forged",
    }
    # alpha is authorized for mock/text + ws-alpha only.
    with system.client() as client:
        response = client.post(CHAT, json=body(task="task-forged", execution=forged))
        assert response.status_code == 200
        run = response.json()["run"]
        assert run["execution"] == forged  # echoed verbatim, not attested
        assert run["preset"] == "mock/text"  # chosen by auth+model, not route
        assert run["workspace_id"] == "ws-alpha"  # authorized workspace only
        assert run["driver_id"] == "mock"
        # A forged route/policy cannot reach a preset the key is denied.
        denied = client.post(
            CHAT,
            json=body(task="task-forged2", model="mock/review", execution=forged),
        )
        assert denied.status_code == 403
        # Nor a workspace outside the principal's scope.
        denied_ws = client.post(
            CHAT,
            json=body(task="task-forged3", workspace="ws-beta", execution=forged),
        )
        assert denied_ws.status_code == 403


def test_execution_context_schema_forbids_unknown_fields():
    """extra='forbid' on the SDK schema is what the API boundary parses with
    (and what a stock Runner's INVALID_PARAMS reject relies on upstream —
    pinned end-to-end in apps/runner/tests/test_execution_context_wire.py)."""
    from pydantic import ValidationError

    from cli_provider_sdk.models import ExecutionContext

    with pytest.raises(ValidationError):
        ExecutionContext.model_validate(dict(EXECUTION, unknown_field="x"))
