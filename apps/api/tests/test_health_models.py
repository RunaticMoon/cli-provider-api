"""Health, authentication, model listing and provider scoping."""

from __future__ import annotations

import httpx

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


def test_health_liveness_and_readiness(system: MockSystem):
    with httpx.Client(timeout=5.0) as client:
        assert client.get(f"{system.base_url}/health/live").json() == {"status": "ok"}
        ready = client.get(f"{system.base_url}/health/ready")
    assert ready.status_code == 200
    # Unauthenticated readiness exposes only a status, never internal topology.
    body = ready.json()
    assert body == {"status": "ready"}


def test_health_ready_detail_requires_authentication(system: MockSystem):
    with system.client() as client:
        ready = client.get("/health/ready")
    assert ready.status_code == 200
    detail = ready.json()["detail"]
    assert detail["runners"]["runner-1"]["ok"] is True
    assert detail["presets"]["mock/text"]["verified"] is True
    # Synthetic opt-in is truthful: it is available but not really verified.
    assert detail["presets"]["mock/text"]["real_verification"] is False


def test_models_expose_truthful_capabilities_and_provenance(system: MockSystem):
    with system.client() as client:
        entry = client.get("/v1/models").json()["data"][0]
    assert entry["id"] == "mock/text"
    assert entry["capabilities"]["streaming"] == "native"
    assert entry["capabilities"]["task_policy"] == "text"
    assert entry["verification"]["status"] != "passed"
    assert entry["real_verification"] is False


def test_request_headers_over_the_configured_bound_are_rejected(system_factory):
    system = system_factory(
        "success",
        config_overrides={"api": {"limits": {"max_headers_bytes": 2048}}},
    )
    with system.client() as client:
        # Positive control: a normal request is unaffected.
        assert client.get("/v1/models").status_code == 200
        oversized = client.get("/v1/models", headers={"X-Pad": "x" * 4096})
    assert oversized.status_code == 431
    assert oversized.json()["error"]["code"] == "headers_too_large"


def test_models_requires_authentication(system: MockSystem):
    with httpx.Client(timeout=5.0) as client:
        response = client.get(f"{system.base_url}/v1/models")
        assert response.status_code == 401
        assert response.json()["error"]["type"] == "authentication_error"


def test_models_are_scoped_to_principal(system: MockSystem):
    with system.client() as client:
        data = client.get("/v1/models").json()["data"]
    assert [m["id"] for m in data] == ["mock/text"]

    with system.client(key=system.beta_key) as beta:
        beta_data = beta.get("/v1/models").json()["data"]
    assert [m["id"] for m in beta_data] == ["mock/review"]


def test_provider_scoped_models(system: MockSystem):
    with system.client() as client:
        scoped = client.get("/providers/mock/v1/models")
        assert scoped.status_code == 200
        assert [m["id"] for m in scoped.json()["data"]] == ["mock/text"]
        unknown = client.get("/providers/does-not-exist/v1/models")
        assert unknown.status_code == 404


def test_invalid_key_is_rejected(system: MockSystem):
    with httpx.Client(
        base_url=system.base_url,
        headers={"Authorization": "Bearer wrong"},
        timeout=5.0,
    ) as client:
        response = client.get("/v1/models")
    assert response.status_code == 401


def test_run_and_artifact_routes_require_authentication(system: MockSystem):
    with httpx.Client(timeout=5.0) as client:
        assert client.get(f"{system.base_url}/api/v1/runs/run_x").status_code == 401
        assert (
            client.get(f"{system.base_url}/api/v1/artifacts/art_x").status_code == 401
        )
        assert (
            client.post(f"{system.base_url}/api/v1/runs/run_x/cancel").status_code == 401
        )
