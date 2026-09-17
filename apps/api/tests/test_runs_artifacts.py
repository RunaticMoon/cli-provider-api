"""Run/event/artifact retrieval, ownership and traversal rejection."""

from __future__ import annotations

from conftest import MockSystem

CHAT = "/v1/chat/completions"


def body(model="mock/text", content="hello", task="task-1", workspace="ws-alpha", **extra):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "metadata": {"task_id": task, "workspace_id": workspace},
    }
    payload.update(extra)
    return payload


def _complete_run(system: MockSystem, content="hello", task="task-1") -> str:
    with system.client() as client:
        response = client.post(CHAT, json=body(content=content, task=task))
        assert response.status_code == 200
        return response.headers["X-Run-Id"]


def test_run_get_and_events_are_owner_scoped(system: MockSystem):
    run_id = _complete_run(system)
    with system.client() as client:
        run = client.get(f"/api/v1/runs/{run_id}")
        assert run.status_code == 200
        assert run.json()["status"] == "completed"
        events = client.get(f"/api/v1/runs/{run_id}/events").json()["events"]
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert events[0]["kind"] == "run.started"
    assert events[-1]["kind"] == "run.completed"

    with system.client(key=system.beta_key) as beta:
        assert beta.get(f"/api/v1/runs/{run_id}").status_code == 404
        assert beta.get(f"/api/v1/runs/{run_id}/events").status_code == 404
        assert beta.post(f"/api/v1/runs/{run_id}/cancel").status_code == 404


def test_unknown_run_id_is_not_found(system: MockSystem):
    with system.client() as client:
        assert client.get("/api/v1/runs/run_doesnotexist").status_code == 404


def test_artifact_persistence_and_readback(system: MockSystem):
    run_id = _complete_run(system, content="persisted text")
    with system.client() as client:
        run = client.get(f"/api/v1/runs/{run_id}").json()
        artifact_id = run["artifacts"][0]
        artifact = client.get(f"/api/v1/artifacts/{artifact_id}")
    assert artifact.status_code == 200
    assert "persisted text" in artifact.text
    assert artifact.headers["content-type"].startswith("text/plain")


def test_artifact_is_owner_scoped(system: MockSystem):
    run_id = _complete_run(system)
    with system.client() as client:
        artifact_id = client.get(f"/api/v1/runs/{run_id}").json()["artifacts"][0]
    with system.client(key=system.beta_key) as beta:
        assert beta.get(f"/api/v1/artifacts/{artifact_id}").status_code == 404


def test_artifact_traversal_ids_are_rejected(system: MockSystem):
    with system.client() as client:
        for bad in ["..", "a/b", "../etc/passwd", "art_ok/../../etc", "%2e%2e%2f"]:
            response = client.get(f"/api/v1/artifacts/{bad}", follow_redirects=False)
            # Never a 200 with content: rejected, or URL-normalised to a redirect.
            assert response.status_code in (400, 404, 307), bad


def test_events_pagination(system: MockSystem):
    run_id = _complete_run(system)
    with system.client() as client:
        first = client.get(f"/api/v1/runs/{run_id}/events?limit=2").json()["events"]
        rest = client.get(f"/api/v1/runs/{run_id}/events?after=2").json()["events"]
    assert len(first) == 2
    assert [e["sequence"] for e in first] == [1, 2]
    assert all(e["sequence"] > 2 for e in rest)
