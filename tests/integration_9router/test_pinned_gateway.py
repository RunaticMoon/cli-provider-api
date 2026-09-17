"""Opt-in, real 9Router 0.5.75 -> HTTP API -> UDS -> synthetic driver.

Run with NINEROUTER_APP pointing to the trusted npm package's app directory:
    uv run --all-packages pytest tests/integration_9router -v
No installed service is contacted, no provider credentials are loaded, and
neither the CLI artifact nor account data is distributed with this repository.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_mock_system():
    path = ROOT / "apps/api/tests/conftest.py"
    spec = importlib.util.spec_from_file_location("cpa_gateway_mock_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.MockSystem


@pytest.fixture
def gateway(tmp_path):
    configured = os.environ.get("NINEROUTER_APP")
    if not configured:
        pytest.skip("NINEROUTER_APP missing: real pinned gateway NOT RUN")
    assert configured is not None
    app = Path(configured).resolve()
    package = json.loads((app / "package.json").read_text())
    assert package["name"] == "9router-app" and package["version"] == "0.5.75"
    node = os.environ.get("NINEROUTER_NODE") or shutil.which("node")
    assert node, "A trusted Node executable is required"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    home, data, api_dir = (tmp_path / name for name in ("home", "router-data", "api"))
    for path in (home, data, api_dir):
        path.mkdir(mode=0o700)
    system = load_mock_system()(
        root=str(api_dir), api_key=secrets.token_urlsafe(32),
        beta_key=secrets.token_urlsafe(32), gamma_key=secrets.token_urlsafe(32),
    )
    password = secrets.token_urlsafe(32)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home), "DATA_DIR": str(data), "PORT": str(port),
        "HOSTNAME": "127.0.0.1", "NODE_ENV": "production",
        "INITIAL_PASSWORD": password, "JWT_SECRET": secrets.token_urlsafe(32),
        "API_KEY_SECRET": secrets.token_urlsafe(32), "MACHINE_ID_SALT": "synthetic",
        "MODEL_CATALOG_SYNC": "off", "ENABLE_REQUEST_LOGS": "false",
    }
    proc = None
    log = (tmp_path / "gateway.log").open("wb")
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False)
    try:
        system.start()
        proc = subprocess.Popen(
            [node, str(app / "custom-server.js")], cwd=tmp_path, env=env,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            assert proc.poll() is None, "isolated gateway exited before readiness"
            try:
                if client.get("/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("isolated gateway readiness deadline")
        assert client.post("/api/auth/login", json={"password": password}).status_code == 200
        settings = {
            "requireLogin": True, "requireApiKey": True, "cloudEnabled": False,
            "tunnelEnabled": False, "tailscaleEnabled": False, "rtkEnabled": False,
            "headroomEnabled": False, "cavemanEnabled": False, "ponytailEnabled": False,
            "pxpipeEnabled": False, "outboundProxyEnabled": False,
            "comboStrategy": "fallback", "fallbackStrategy": "fill-first",
        }
        assert client.patch("/api/settings", json=settings).status_code == 200
        key = client.post("/api/keys", json={"name": "synthetic-integration"})
        assert key.status_code == 201
        client.headers["Authorization"] = "Bearer " + key.json()["key"]
        for prefix, token in (("badfixture", "deliberately-invalid-fixture"), ("goodfixture", system.api_key)):
            node_response = client.post("/api/provider-nodes", json={
                "type": "openai-compatible", "apiType": "chat", "name": prefix,
                "prefix": prefix, "baseUrl": system.base_url + "/providers/mock/v1",
            })
            assert node_response.status_code == 201
            assert client.post("/api/providers", json={
                "provider": node_response.json()["node"]["id"], "apiKey": token, "name": prefix,
            }).status_code == 201
        candidates = ["badfixture/mock/text", "goodfixture/mock/text"]
        response = client.post("/api/combos", json={"name": "cpa.fixture", "models": candidates})
        assert response.status_code in (200, 201)
        combos = client.get("/api/combos").json()
        values = combos if isinstance(combos, list) else combos["combos"]
        assert any(x["name"] == "cpa.fixture" and x["models"] == candidates for x in values)
        yield client, system
    finally:
        client.close()
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=8)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
        system.stop()
        log.close()
        if proc is not None:
            assert proc.poll() is not None
        for process in (system.api_proc, system.runner_proc):
            if process is not None:
                assert process.poll() is not None


def request(task_id, *, model="goodfixture/mock/text", stream=False):
    return {
        "model": model, "stream": stream,
        "messages": [{"role": "system", "content": "synthetic system"},
                     {"role": "assistant", "content": "synthetic prior answer"},
                     {"role": "user", "content": "synthetic task"}],
        "metadata": {"task_id": task_id, "workspace_id": "ws-alpha"},
    }


def test_preexecution_fallback_identity_artifact_and_replay(gateway):
    client, system = gateway
    body = request("gateway-task", model="cpa.fixture")
    response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200
    run = response.json()["run"]
    assert run["status"] == "completed"
    assert run["task_id"] == "gateway-task" and run["workspace_id"] == "ws-alpha"
    assert run["run_id"] and run["artifacts"]
    with system.client() as management:
        assert management.get("/api/v1/runs/" + run["run_id"]).status_code == 200
        assert management.get("/api/v1/runs/" + run["run_id"] + "/events").status_code == 200
        artifact = management.get("/api/v1/artifacts/" + run["artifacts"][0])
        assert artifact.status_code == 200 and artifact.content
    replay = client.post("/v1/chat/completions", json={**body, "model": "goodfixture/mock/text"})
    assert replay.status_code == 200
    assert replay.json()["run"]["run_id"] == run["run_id"]
    assert replay.json()["run"]["cached"] is True
    changed = {**body, "model": "goodfixture/mock/text", "messages": [{"role": "user", "content": "different"}]}
    assert client.post("/v1/chat/completions", json=changed).status_code != 200


def test_streaming_run_identity_survives_header_loss(gateway):
    client, system = gateway
    response = client.post("/v1/chat/completions", json=request("gateway-stream", stream=True))
    assert response.status_code == 200
    frames = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert "[DONE]" in frames
    objects = [json.loads(line) for line in frames if line != "[DONE]"]
    assert not any("error" in value for value in objects)
    run_id = objects[0].get("run_id") or objects[0].get("run", {}).get("run_id")
    if not run_id and objects[0].get("id", "").startswith("chatcmpl-run_"):
        run_id = objects[0]["id"][len("chatcmpl-"):]
    assert run_id, "first SSE chunk must identify run via metadata or the documented bound completion id"
    results = [value["run"] for value in objects if "run" in value and value["run"].get("status") == "completed"]
    assert results and results[-1]["run_id"] == run_id
    assert results[-1]["verification"]["status"] == "not_run"
    with system.client() as management:
        assert management.get("/api/v1/runs/" + run_id).status_code == 200
