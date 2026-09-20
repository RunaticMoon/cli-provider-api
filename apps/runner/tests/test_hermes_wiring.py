"""End-to-end wiring: real Runner subprocess -> allowlisted Hermes-API driver.

The driver is non-synthetic: it refuses to run at all without an operator
execution config, refuses ``--yolo`` unless the bound workspace grants
``hermes.yolo``, and refuses to spawn when the provider catalog check fails.
The catalog endpoint and provider traffic are loopback fixtures only — the
``HERMES_API_*_BASE_URL`` operator envs pin the origin, keys are synthetic,
and no real provider or account is ever contacted.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cli_provider_runner.client import RunnerClient

from conftest import run_params

DRIVER_TESTS = (
    Path(__file__).parent.parent.parent.parent / "drivers" / "hermes-api" / "tests"
)
FAKE_HERMES = DRIVER_TESTS / "fake_hermes.py"
OPENAI_MOCK = DRIVER_TESTS / "fixtures" / "openai_mock.py"
HERMES_BIN = (
    os.environ.get("HERMES_API_TEST_CLI")
    or shutil.which("hermes")
    or "/home/ubuntu/.local/bin/hermes"
)

BAI_PRESET = "bai/deepseek-v4.1-flash"
CC_PRESET = "commandcode/deepseek-v4.1-flash"
SECRET = "sk-runner-fixture-000"


# --------------------------------------------------------- loopback catalog


class _CatalogHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        return None

    def do_GET(self) -> None:
        catalog = self.server.catalog  # type: ignore[attr-defined]
        catalog.hits.append(self.path)
        ids = (
            ["deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash"]
            if catalog.mode == "ok"
            else ["someone/else-model"]
        )
        body = json.dumps(
            {"object": "list", "data": [{"id": mid} for mid in ids]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CatalogServer:
    def __init__(self) -> None:
        self.mode = "ok"
        self.hits: list[str] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _CatalogHandler)
        self._server.catalog = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def catalog():
    server = CatalogServer()
    yield server
    server.close()


# --------------------------------------------------------------- runner env


def write_execution_config(
    tmp_path: Path, root: Path, *, actions: list[str] | None = None
) -> str:
    conf_dir = tmp_path / "protected"
    conf_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(conf_dir, 0o700)
    conf = conf_dir / "execution.json"
    conf.write_text(
        json.dumps(
            {
                "version": 1,
                "workspaces": {
                    "ws-1": {
                        "root": str(root),
                        "allowed_actions": list(actions or []),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    conf.chmod(0o600)
    return str(conf)


def hermes_env(tmp_path: Path, catalog_url: str, mode: str = "ok") -> dict:
    """Runner-process env pointing the Hermes driver at loopback fixtures."""
    wrapper = tmp_path / "hermes"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_HERMES}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return {
        "HERMES_API_CLI": str(wrapper),
        "HERMES_API_BAI_BASE_URL": catalog_url,
        "HERMES_API_COMMANDCODE_BASE_URL": catalog_url,
        "HERMES_API_CATALOG_TTL": "0",
        "BAI_API_KEY": SECRET,
        "COMMANDCODE_API_KEY": SECRET,
        "FAKE_HERMES_MODE": mode,
        "FAKE_HERMES_CAPTURE": str(tmp_path / "capture.json"),
    }


def toy_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws-root"
    root.mkdir(parents=True, exist_ok=True)
    (root / "input.txt").write_text("fixture-input\n", encoding="utf-8")
    return root


async def drive(client: RunnerClient, params: dict) -> list:
    return [envelope async for envelope in client.run(params)]


# -------------------------------------------------------------------- tests


async def test_hermes_run_completes_through_bound_workspace(
    runner_factory, tmp_path, catalog
):
    root = toy_workspace(tmp_path)
    runner = runner_factory(
        "success",
        driver_id="hermes-api",
        distribution="cli-driver-hermes-api",
        version="0.1.0",
        extra_env=hermes_env(tmp_path, catalog.base_url),
        execution_config=write_execution_config(
            tmp_path, root, actions=["hermes.yolo"]
        ),
    )
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client, run_params(preset=BAI_PRESET, deadline_seconds=15.0)
        )
        result = client.last_run_response.result
        assert result["status"] == "completed", result
        assert events[-1].event.kind == "run.completed"
        # The catalog check ran in the runner process against the loopback.
        assert catalog.hits == ["/v1/models"]
        # The spawned CLI saw the bound workspace and only the selected key.
        capture = json.loads((tmp_path / "capture.json").read_text())
        assert capture["cwd"] == str(root)
        assert capture["env_seen"]["BAI_API_KEY"] is True
        assert capture["env_seen"]["COMMANDCODE_API_KEY"] is False
    finally:
        await client.aclose()


async def test_hermes_run_is_denied_without_the_yolo_grant(
    runner_factory, tmp_path, catalog
):
    root = toy_workspace(tmp_path)
    runner = runner_factory(
        "success",
        driver_id="hermes-api",
        distribution="cli-driver-hermes-api",
        version="0.1.0",
        extra_env=hermes_env(tmp_path, catalog.base_url),
        execution_config=write_execution_config(tmp_path, root, actions=[]),
    )
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client, run_params(preset=BAI_PRESET, deadline_seconds=15.0)
        )
        result = client.last_run_response.result
        assert result["status"] == "failed"
        assert events[-1].event.payload.code == "yolo_not_preapproved"
        # Denied before any effect: no CLI spawn, not even a catalog fetch.
        assert not (tmp_path / "capture.json").exists()
        assert catalog.hits == []
    finally:
        await client.aclose()


async def test_hermes_run_is_denied_without_an_execution_config(
    runner_factory, tmp_path, catalog
):
    toy_workspace(tmp_path)
    runner = runner_factory(
        "success",
        driver_id="hermes-api",
        distribution="cli-driver-hermes-api",
        version="0.1.0",
        extra_env=hermes_env(tmp_path, catalog.base_url),
    )
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client, run_params(preset=BAI_PRESET, deadline_seconds=15.0)
        )
        assert client.last_run_response.result["status"] == "failed"
        assert events[-1].event.payload.code == "execution_config_required"
        assert not (tmp_path / "capture.json").exists()
        assert catalog.hits == []
    finally:
        await client.aclose()


async def test_hermes_run_is_denied_when_catalog_membership_is_lost(
    runner_factory, tmp_path, catalog
):
    root = toy_workspace(tmp_path)
    runner = runner_factory(
        "success",
        driver_id="hermes-api",
        distribution="cli-driver-hermes-api",
        version="0.1.0",
        extra_env=hermes_env(tmp_path, catalog.base_url),
        execution_config=write_execution_config(
            tmp_path, root, actions=["hermes.yolo"]
        ),
    )
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client, run_params(preset=BAI_PRESET, deadline_seconds=15.0)
        )
        assert client.last_run_response.result["status"] == "completed"
        assert (tmp_path / "capture.json").exists()

        # Provider drops the model; the next run must fail before any spawn.
        catalog.mode = "missing"
        (tmp_path / "capture.json").unlink()
        events = await drive(
            client,
            run_params(run_id="run-2", preset=BAI_PRESET, deadline_seconds=15.0),
        )
        assert client.last_run_response.result["status"] == "failed"
        assert events[-1].event.payload.code == "catalog_not_verified"
        assert not (tmp_path / "capture.json").exists()
        assert catalog.hits[-1] == "/v1/models"  # membership was rechecked
    finally:
        await client.aclose()


# ------------------------------------------------- real CLI through runner


def start_provider_mock(tmp_path: Path) -> tuple[subprocess.Popen, str, Path]:
    log = tmp_path / "provider-wire.jsonl"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    env = dict(os.environ)
    env["HERMES_MOCK_LOG"] = str(log)
    proc = subprocess.Popen([sys.executable, str(OPENAI_MOCK), str(port)], env=env)
    for _ in range(100):
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            sock.close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        raise RuntimeError("mock provider did not start")
    return proc, f"http://127.0.0.1:{port}", log


@pytest.mark.skipif(
    not Path(HERMES_BIN).exists(), reason="hermes CLI not installed"
)
async def test_real_hermes_through_runner_uses_the_bound_workspace(
    runner_factory, tmp_path
):
    """Real ``hermes`` binary, loopback OpenAI provider, operator-bound toy
    workspace with the explicit ``hermes.yolo`` grant — the full path the
    production Runner takes, minus only the provider origin."""
    root = toy_workspace(tmp_path)
    mock, base_url, log = start_provider_mock(tmp_path)
    runner = runner_factory(
        "success",
        driver_id="hermes-api",
        distribution="cli-driver-hermes-api",
        version="0.1.0",
        extra_env={
            "HERMES_API_CLI": HERMES_BIN,
            "HERMES_API_BAI_BASE_URL": base_url,
            "HERMES_API_COMMANDCODE_BASE_URL": base_url,
            "BAI_API_KEY": SECRET,
            "COMMANDCODE_API_KEY": SECRET,
            "HERMES_API_MAX_TURNS": "8",
            "HERMES_API_RUN_BUDGET": "120",
        },
        execution_config=write_execution_config(
            tmp_path, root, actions=["hermes.yolo"]
        ),
    )
    client = await RunnerClient.connect(runner.socket_path)
    try:
        params = run_params(
            preset=BAI_PRESET,
            deadline_seconds=180.0,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Read input.txt, then write agent-out.txt containing "
                        "fixture-agent-wrote-this, then answer briefly."
                    ),
                }
            ],
        )
        events = await drive(client, params)
        result = client.last_run_response.result
        assert result["status"] == "completed", result
        assert (root / "agent-out.txt").read_text() == "fixture-agent-wrote-this"
        wire = [
            json.loads(line) for line in log.read_text().splitlines()
        ]
        # Catalog membership checked with auth before any chat call.
        catalog_hits = [r for r in wire if r.get("_path", "").endswith("/models")]
        assert catalog_hits and all(r["_auth"] for r in catalog_hits)
        chats = [
            r for r in wire if r.get("_path", "").endswith("/chat/completions")
        ]
        assert chats and all(
            r["model"] == "deepseek-v4.1-flash" for r in chats
        )
    finally:
        await client.aclose()
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()
