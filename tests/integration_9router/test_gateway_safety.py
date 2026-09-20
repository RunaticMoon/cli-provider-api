"""Opt-in, real 9Router 0.5.81 -> HTTP API -> UDS -> retry-safety fixture.

Both provider nodes authenticate to the API with the SAME principal key, so
sequential-fallback candidates share the exact API principal + task identity +
Store — the precondition for the durable cross-candidate admission guard. The
fixture stand-in (fixture_runner.py) proves execution counts on disk:
``runs.ndjson`` = run RPCs received, ``effects.ndjson`` = agent starts.

Run with NINEROUTER_APP pointing at the pinned 0.5.81 app directory. No
installed service (port 20128) is contacted; everything runs on a temporary
port, HOME and DATA_DIR.
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
PINNED_VERSION = "0.5.81"


def load_fixture_system():
    path = ROOT / "tests/integration_9router/conftest.py"
    spec = importlib.util.spec_from_file_location("cpa_gateway_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.FixtureSystem


def gateway_request(task_id, *, model, stream=False, execution="sentinel"):
    metadata = {"task_id": task_id, "workspace_id": "ws-alpha"}
    if execution != "sentinel":
        metadata["execution"] = execution
    return {
        "model": model,
        "stream": stream,
        "messages": [{"role": "user", "content": "synthetic task"}],
        "metadata": metadata,
    }


def make_gateway(tmp_path, *, plan):
    """Boot an isolated 9Router + FixtureSystem with a two-candidate combo.

    Returns (client, system, combo_name). Both provider nodes use the same API
    key, so every candidate shares principal `alpha`.
    """
    configured = os.environ.get("NINEROUTER_APP")
    if not configured:
        pytest.skip("NINEROUTER_APP missing: real pinned gateway NOT RUN")
    app = Path(configured).resolve()
    package = json.loads((app / "package.json").read_text())
    assert package["name"] == "9router-app" and package["version"] == PINNED_VERSION
    node = os.environ.get("NINEROUTER_NODE") or shutil.which("node")
    assert node, "A trusted Node executable is required"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    home, data, api_dir = (tmp_path / name for name in ("home", "router-data", "api"))
    for path in (home, data, api_dir):
        path.mkdir(mode=0o700)
    FixtureSystem = load_fixture_system()
    api_key = secrets.token_urlsafe(32)
    system = FixtureSystem(root=str(api_dir), api_key=api_key, plan=plan)
    password = secrets.token_urlsafe(32)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home), "DATA_DIR": str(data), "PORT": str(port),
        "HOSTNAME": "127.0.0.1", "NODE_ENV": "production",
        "INITIAL_PASSWORD": password, "JWT_SECRET": secrets.token_urlsafe(32),
        "API_KEY_SECRET": secrets.token_urlsafe(32), "MACHINE_ID_SALT": "synthetic",
        "MODEL_CATALOG_SYNC": "off", "ENABLE_REQUEST_LOGS": "false",
    }
    log = (tmp_path / "gateway.log").open("wb")
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False
    )
    proc = None
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
        # Both candidates share the exact same API principal key: a fallback
        # candidate carries identical task identity into the same Store.
        for prefix in ("canda", "candb"):
            node_response = client.post("/api/provider-nodes", json={
                "type": "openai-compatible", "apiType": "chat", "name": prefix,
                "prefix": prefix,
                "baseUrl": system.base_url + "/providers/fixture/v1",
            })
            assert node_response.status_code == 201
            assert client.post("/api/providers", json={
                "provider": node_response.json()["node"]["id"],
                "apiKey": api_key, "name": prefix,
            }).status_code == 201
        candidates = ["canda/fixture/alpha", "candb/fixture/alpha"]
        response = client.post(
            "/api/combos", json={"name": "cpa.safety", "models": candidates}
        )
        assert response.status_code in (200, 201)
        return proc, client, system, log
    except BaseException:
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
        system.stop()
        log.close()
        raise


def stop_gateway(proc, client, system, log) -> None:
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


@pytest.fixture
def gateway(tmp_path):
    proc, client, system, log = make_gateway(tmp_path, plan={"default": "success"})
    try:
        yield client, system
    finally:
        stop_gateway(proc, client, system, log)


@pytest.fixture
def gateway_factory(tmp_path):
    started = []

    def make(plan):
        path = tmp_path / f"gw{len(started)}"
        path.mkdir()
        proc, client, system, log = make_gateway(path, plan=plan)
        started.append((proc, client, system, log))
        return client, system

    yield make
    for proc, client, system, log in started:
        stop_gateway(proc, client, system, log)


EXECUTION = {
    "task_revision": "rev-7",
    "base_revision": "base-2026.09",
    "route": "worker.code.standard",
    "policy_version": "pol-3",
}


def test_gateway_preflight_rejection_falls_back_once(gateway_factory):
    """Candidate A is rejected before dispatch; B executes exactly once and A
    proves zero agent effects on disk."""
    client, system = gateway_factory(
        {"presets": {"fixture/alpha": ["reject", "success"]}}
    )
    response = client.post(
        "/v1/chat/completions",
        json=gateway_request("gw-preflight", model="cpa.safety"),
    )
    assert response.status_code == 200, response.text[:500]
    run = response.json().get("run") or {}
    received = system.runs_received()
    # A's run RPC reached the fixture and was rejected before dispatch; B ran.
    assert len(received) == 2
    assert received[0]["dispatched"] is False
    assert received[1]["dispatched"] is True
    effects = system.effects()
    assert len(effects) == 1
    assert effects[0]["run_id"] == received[1]["run_id"]
    if run:
        assert run["run_id"] == received[1]["run_id"]


def test_gateway_effectful_failure_never_starts_second_agent(gateway_factory):
    """Candidate A performs its fixture effect then fails. 9Router may issue a
    second HTTP request for B, but the durable admission guard refuses it
    before any runner contact: zero second agent starts, zero file changes,
    and the original run identity survives for control readback."""
    client, system = gateway_factory({"default": "fail"})
    response = client.post(
        "/v1/chat/completions",
        json=gateway_request("gw-effectful", model="cpa.safety"),
    )
    # The gateway surfaces a failure to its client; it must never be a silent
    # success from a second agent.
    assert response.status_code != 200

    effects = system.effects()
    received = system.runs_received()
    assert len(effects) == 1
    # Candidate B never reached the worker fixture at all.
    assert len(received) == 1
    original = effects[0]["run_id"]

    with system.client() as management:
        run = management.get(f"/api/v1/runs/{original}").json()
        assert run["status"] == "failed" and run["outcome"] == "provider_error"
        assert run["task_id"] == "gw-effectful"

        # The blocked admission is durable: a direct retry still conflicts.
        retry = management.post(
            "/v1/chat/completions",
            json=gateway_request("gw-effectful", model="fixture/alpha"),
        )
        assert retry.status_code == 409
        assert retry.json()["error"]["code"] == "run_not_retryable"
    assert len(system.effects()) == 1


def test_gateway_dropped_candidate_never_starts_second_agent(gateway_factory):
    """Transport dies mid-run on candidate A: held unknown, never retried."""
    client, system = gateway_factory(
        {"default": "drop"},
    )
    # Tighten nothing: the API detects the dropped UDS stream immediately.
    response = client.post(
        "/v1/chat/completions",
        json=gateway_request("gw-drop", model="cpa.safety"),
    )
    assert response.status_code != 200
    assert len(system.effects()) == 1
    assert len(system.runs_received()) == 1


def test_gateway_execution_metadata_reaches_worker(gateway):
    client, system = gateway
    response = client.post(
        "/v1/chat/completions",
        json=gateway_request(
            "gw-meta", model="cpa.safety", execution=EXECUTION, stream=True
        ),
    )
    assert response.status_code == 200, response.text[:500]
    received = system.runs_received()
    assert len(received) == 1
    assert received[0]["execution"] == EXECUTION
    assert received[0]["task_id"] == "gw-meta"
    assert system.effects()[0]["execution"] == EXECUTION
    # Custom headers may be dropped by the gateway; run identity is carried in
    # the SSE/JSON body extension and verified by control readback.
    run_id = received[0]["run_id"]
    with system.client() as management:
        run = management.get(f"/api/v1/runs/{run_id}").json()
        assert run["execution"] == EXECUTION
        assert run["status"] == "completed"
