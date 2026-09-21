"""Full-stack: real API subprocess -> real Runner subprocess -> synthetic ``agy``.

Proves the whole path a production request takes: HTTP admission, controller
model binding, UDS run, allowlisted Antigravity driver, and the synthetic
stream-json CLI fixture. No real CLI, credential or inference is involved; every
writable path is a pytest tmp dir.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

from cli_provider_core import hash_api_key

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DRIVER_FIXTURE = (
    Path(REPO_ROOT)
    / "drivers"
    / "antigravity"
    / "tests"
    / "fixtures"
    / "fake_agy.py"
)

MODEL = "gemini-3.8-flash-high"
MODEL_2 = "claude-opus-4-6-thinking"
PINNED_VERSION = "1.2.7"
API_KEY = "local-alpha-key"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_live(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    base = f"http://127.0.0.1:{port}"
    with httpx.Client(timeout=2.0) as client:
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("api exited early")
            try:
                if client.get(f"{base}/health/live").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    raise TimeoutError("api never became live")


def _wait_for_verified(port: int, timeout: float = 30.0) -> None:
    """Poll /v1/models until the pinned preset reports verification passed.

    The overall /health/ready gate is intentionally NOT used: the config keeps
    one enabled preset whose model is deliberately absent from the driver
    allowlist, so the system is expected to stay not-ready forever.
    """
    deadline = time.time() + timeout
    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=5.0,
    ) as client:
        while time.time() < deadline:
            try:
                data = client.get("/v1/models").json().get("data", [])
            except Exception:
                time.sleep(0.1)
                continue
            for entry in data:
                if (
                    entry["id"] == f"antigravity/{MODEL}"
                    and (entry.get("verification") or {}).get("status") == "passed"
                ):
                    return
            time.sleep(0.2)
    raise TimeoutError("pinned preset never verified")


def _wait_for_socket(path: str, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(f"runner exited early: {err.strip()}")
        if os.path.exists(path):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(path)
                return
            except OSError:
                pass
            finally:
                probe.close()
        time.sleep(0.05)
    raise TimeoutError("runner socket never became ready")


@pytest.fixture
def agy_system(tmp_path):
    """Real API + real antigravity Runner against the synthetic fixture."""
    wrapper = tmp_path / "agy"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{DRIVER_FIXTURE}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    conf_dir = tmp_path / "protected"
    conf_dir.mkdir(mode=0o700)
    exec_conf = conf_dir / "execution.json"
    exec_conf.write_text(
        json.dumps(
            {
                "version": 1,
                "workspaces": {
                    "ws-alpha": {"root": str(tmp_path), "allowed_actions": []}
                },
            }
        ),
        encoding="utf-8",
    )
    exec_conf.chmod(0o600)

    socket_path = str(tmp_path / "runner.sock")
    port = _free_port()
    config = {
        "schema_version": 1,
        "data_dir": str(tmp_path / "data"),
        "api": {
            "host": "127.0.0.1",
            "port": port,
            "default_run_deadline_seconds": 15.0,
            "max_run_deadline_seconds": 30.0,
        },
        "runners": [
            {
                "instance_id": "runner-1",
                "driver_id": "antigravity",
                "driver_version": "0.1.0",
                "distribution": "cli-driver-antigravity",
                "socket_path": socket_path,
                "connect_timeout_seconds": 5.0,
            }
        ],
        "presets": [
            {
                "alias": f"antigravity/{MODEL}",
                "runner_ref": "runner-1",
                "model_id": MODEL,
            },
            {
                "alias": f"antigravity/{MODEL_2}",
                "runner_ref": "runner-1",
                "model_id": MODEL_2,
            },
            {
                # In the catalog but NOT in the driver's operator allowlist:
                # it must never become available.
                "alias": "antigravity/gemini-3.8-flash-medium",
                "runner_ref": "runner-1",
                "model_id": "gemini-3.8-flash-medium",
            },
            {
                # Noncanonical alias: the model binding can only arrive via the
                # forwarded model_id, never via the preset name.
                "alias": "agy-cli/flash",
                "runner_ref": "runner-1",
                "model_id": MODEL,
            },
        ],
        "workspaces": [{"workspace_id": "ws-alpha"}],
        "principals": [
            {
                "name": "alpha",
                "key_hash": hash_api_key(API_KEY),
                "allowed_presets": [
                    f"antigravity/{MODEL}",
                    f"antigravity/{MODEL_2}",
                    "antigravity/gemini-3.8-flash-medium",
                    "agy-cli/flash",
                ],
                "allowed_workspaces": ["ws-alpha"],
                "max_concurrency": 1,
            }
        ],
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "AGY_CLI": str(wrapper),
            "AGY_MODELS": f"{MODEL},{MODEL_2}",
            "AGY_EXPECTED_VERSION": PINNED_VERSION,
            "FAKE_AGY_MODE": "ok",
            "FAKE_AGY_CATALOG": "ok",
            "FAKE_AGY_LOG": str(tmp_path / "fixture.log"),
        }
    )

    runner_proc = subprocess.Popen(
        [
            sys.executable, "-m", "cli_provider_runner", "serve",
            "--socket", socket_path,
            "--instance-id", "runner-1",
            "--driver-id", "antigravity",
            "--distribution", "cli-driver-antigravity",
            "--version", "0.1.0",
            "--execution-config", str(exec_conf),
        ],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        _wait_for_socket(socket_path, runner_proc)
        api_log = open(tmp_path / "api.log", "w", encoding="utf-8")
        api_proc = subprocess.Popen(
            [
                sys.executable, "-m", "cli_provider_api", "serve",
                "--config", str(config_path),
                "--host", "127.0.0.1", "--port", str(port),
            ],
            cwd=REPO_ROOT, env=env,
            stdout=api_log, stderr=subprocess.STDOUT, text=True,
        )
        try:
            _wait_for_live(port, api_proc)
            _wait_for_verified(port)
        except Exception:
            api_proc.kill()
            raise
    except Exception:
        runner_proc.kill()
        raise

    class System:
        base_url = f"http://127.0.0.1:{port}"

        def client(self) -> httpx.Client:
            return httpx.Client(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=15.0,
            )

    yield System()

    for proc in (api_proc, runner_proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except Exception:
                    pass
    api_log.close()


def _chat_body(model: str) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "summarize the module"}],
        "metadata": {"task_id": "task-1", "workspace_id": "ws-alpha"},
    }


def test_models_endpoint_lists_only_verified_exact_ids(agy_system):
    with agy_system.client() as client:
        response = client.get("/v1/models")
    assert response.status_code == 200
    ids = {entry["id"] for entry in response.json()["data"]}
    # The allowlisted, catalog-observed ids are available; the catalog-only
    # id that is not in the operator allowlist is not promoted.
    assert f"antigravity/{MODEL}" in ids
    assert f"antigravity/{MODEL_2}" in ids
    assert "antigravity/gemini-3.8-flash-medium" not in ids
    entries = {e["id"]: e for e in response.json()["data"]}
    assert entries[f"antigravity/{MODEL}"]["verification"]["status"] == "passed"
    assert entries[f"antigravity/{MODEL}"]["real_verification"] is True


def test_chat_completion_runs_through_antigravity_runner(agy_system):
    with agy_system.client() as client:
        response = client.post("/v1/chat/completions", json=_chat_body(f"antigravity/{MODEL}"))
    assert response.status_code == 200
    data = response.json()
    assert data["choices"][0]["message"]["content"] == "hello world"
    assert data["usage"]["prompt_tokens"] == 11
    assert data["usage"]["completion_tokens"] == 2
    run = data["run"]
    assert run["status"] == "completed" and run["outcome"] == "succeeded"
    assert run["synthetic"] is False
    assert run["usage"]["provenance"] == "reported"

    # The generic binding path forwards the preset's pinned model_id as
    # model_alias; the run.started event proves the admitted binding reached
    # the driver verbatim.
    with agy_system.client() as client:
        events = client.get(f"/api/v1/runs/{run['run_id']}/events").json()["events"]
    started = next(e for e in events if e["kind"] == "run.started")
    assert started["payload"]["model_alias"] == MODEL
    assert started["payload"]["preset"] == f"antigravity/{MODEL}"


def test_chat_completion_second_exact_preset(agy_system):
    with agy_system.client() as client:
        response = client.post(
            "/v1/chat/completions", json=_chat_body(f"antigravity/{MODEL_2}")
        )
    assert response.status_code == 200
    assert response.json()["run"]["status"] == "completed"


def test_model_alias_binding_reaches_driver_for_noncanonical_preset(agy_system):
    # The preset alias does not encode the model; only the controller-forwarded
    # model_id can bind it. Regression for the forwarding gap in the run params.
    with agy_system.client() as client:
        response = client.post("/v1/chat/completions", json=_chat_body("agy-cli/flash"))
    assert response.status_code == 200
    run = response.json()["run"]
    assert run["status"] == "completed"
    with agy_system.client() as client:
        events = client.get(f"/api/v1/runs/{run['run_id']}/events").json()["events"]
    started = next(e for e in events if e["kind"] == "run.started")
    assert started["payload"]["model_alias"] == MODEL


def test_unverified_preset_is_unavailable_not_degraded(agy_system):
    with agy_system.client() as client:
        response = client.post(
            "/v1/chat/completions",
            json=_chat_body("antigravity/gemini-3.8-flash-medium"),
        )
    assert response.status_code == 503
