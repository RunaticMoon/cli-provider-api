"""Fixture system for 9Router retry-safety integration tests.

``FixtureSystem`` boots the real API subprocess against ``fixture_runner.py`` —
a real UDS/NDJSON Runner-protocol stand-in under this directory (never the
production Runner and never the mock driver). The fixture records every ``run``
RPC and every agent start under ``<root>/`` so tests can prove exactly how many
executions a request produced.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import yaml

from cli_provider_core import hash_api_key

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FIXTURE_RUNNER = os.path.join(os.path.dirname(__file__), "fixture_runner.py")

CHAT = "/v1/chat/completions"

EXECUTION = {
    "task_revision": "rev-7",
    "base_revision": "base-2026.09",
    "route": "worker.code.standard",
    "policy_version": "pol-3",
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def chat_body(
    task="task-1",
    model="fixture/alpha",
    workspace="ws-alpha",
    content="hello",
    execution="sentinel",
    stream=False,
    **extra,
):
    metadata = {"task_id": task, "workspace_id": workspace}
    if execution != "sentinel":
        metadata["execution"] = execution
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "metadata": metadata,
    }
    if stream:
        payload["stream"] = True
    payload.update(extra)
    return payload


@dataclass
class FixtureSystem:
    """Real API subprocess + fixture Runner stand-in over a real UDS."""

    root: str
    api_key: str = "fixture-alpha-key"
    plan: dict[str, Any] = field(default_factory=lambda: {"default": "success"})
    config_overrides: dict[str, Any] = field(default_factory=dict)
    port: int = 0
    api_proc: subprocess.Popen | None = None
    runner_proc: subprocess.Popen | None = None
    _log: Any = None
    _config_path: str = ""

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def socket_path(self) -> str:
        return os.path.join(self.root, "runner.sock")

    @property
    def fixture_root(self) -> str:
        return os.path.join(self.root, "fixture")

    # ------------------------------------------------------------- evidence

    def spool(self, name: str) -> list[dict[str, Any]]:
        path = os.path.join(self.fixture_root, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def runs_received(self) -> list[dict[str, Any]]:
        return self.spool("runs.ndjson")

    def effects(self) -> list[dict[str, Any]]:
        """Agent starts — the synthetic file effect proof."""
        return self.spool("effects.ndjson")

    def write_plan(self, plan: dict[str, Any]) -> None:
        """Rewrite the live behaviour plan (the fixture re-reads it per run)."""
        os.makedirs(self.fixture_root, exist_ok=True)
        with open(
            os.path.join(self.fixture_root, "plan.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump(plan, handle)

    def _config(self) -> dict[str, Any]:
        config = {
            "schema_version": 1,
            "data_dir": os.path.join(self.root, "data"),
            "api": {
                "host": "127.0.0.1",
                "port": self.port,
                "default_run_deadline_seconds": 5.0,
                "max_run_deadline_seconds": 10.0,
                "cancel_deadline_seconds": 1.0,
                "keepalive_seconds": 0.3,
                "limits": {"max_body_bytes": 8192},
                "concurrency": {
                    "per_runner": 2,
                    "per_principal": 2,
                    "queue_timeout_seconds": 2.0,
                },
            },
            "runners": [
                {
                    "instance_id": "runner-1",
                    "driver_id": "fixture",
                    "driver_version": "0.1.0",
                    "distribution": "test-fixture",
                    "socket_path": self.socket_path,
                    "connect_timeout_seconds": 5.0,
                }
            ],
            "presets": [
                {
                    "alias": "fixture/alpha",
                    "runner_ref": "runner-1",
                    "model_id": "fixture-model",
                    "allow_synthetic_unverified": True,
                },
                {
                    "alias": "fixture/beta",
                    "runner_ref": "runner-1",
                    "model_id": "fixture-model",
                    "allow_synthetic_unverified": True,
                },
            ],
            "workspaces": [{"workspace_id": "ws-alpha"}],
            "principals": [
                {
                    "name": "alpha",
                    "key_hash": hash_api_key(self.api_key),
                    "allowed_presets": ["fixture/alpha", "fixture/beta"],
                    "allowed_workspaces": ["ws-alpha"],
                    "max_concurrency": 2,
                },
            ],
        }
        _merge(config, self.config_overrides)
        return config

    # ------------------------------------------------------------- lifecycle

    def start(self) -> "FixtureSystem":
        self.port = _free_port()
        os.makedirs(self.fixture_root, exist_ok=True)
        self.write_plan(self.plan)
        self._config_path = os.path.join(self.root, "config.yaml")
        with open(self._config_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(self._config(), handle)

        self.runner_proc = subprocess.Popen(
            [
                sys.executable,
                FIXTURE_RUNNER,
                "--socket",
                self.socket_path,
                "--root",
                self.fixture_root,
                "--instance-id",
                "runner-1",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._wait_for_socket()
        self._start_api()
        return self

    def _start_api(self) -> None:
        self._log = open(os.path.join(self.root, "api.log"), "a", encoding="utf-8")
        self.api_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "cli_provider_api",
                "serve",
                "--config",
                self._config_path,
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=ROOT,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._wait_for_ready()

    def kill_api(self) -> None:
        if self.api_proc is not None and self.api_proc.poll() is None:
            self.api_proc.kill()
            self.api_proc.wait(timeout=10)

    def restart_api(self) -> None:
        """Restart the API against the same DB/config (fixture keeps running)."""
        self.kill_api()
        self._start_api()

    def _wait_for_socket(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.runner_proc is not None and self.runner_proc.poll() is not None:
                err = (
                    self.runner_proc.stderr.read()
                    if self.runner_proc.stderr
                    else ""
                )
                raise RuntimeError(f"fixture runner exited early: {err.strip()}")
            if os.path.exists(self.socket_path):
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    probe.connect(self.socket_path)
                    return
                except OSError:
                    pass
                finally:
                    probe.close()
            time.sleep(0.05)
        raise TimeoutError("fixture runner socket never became ready")

    def _wait_for_ready(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        with httpx.Client(timeout=2.0) as client:
            while time.time() < deadline:
                if self.api_proc is not None and self.api_proc.poll() is not None:
                    raise RuntimeError(f"api exited early: {self._read_log()}")
                try:
                    live = client.get(f"{self.base_url}/health/live")
                    ready = client.get(f"{self.base_url}/health/ready")
                    if live.status_code == 200 and ready.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        raise TimeoutError(f"api never became ready: {self._read_log()}")

    def _read_log(self) -> str:
        path = os.path.join(self.root, "api.log")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return handle.read()[-4000:]
        return ""

    def stop(self) -> None:
        for proc in (self.api_proc, self.runner_proc):
            if proc is None:
                continue
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
        if self._log is not None and not self._log.closed:
            self._log.close()

    # --------------------------------------------------------------- client

    def client(self, key: str | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {key or self.api_key}"},
            timeout=15.0,
        )


@pytest.fixture
def fixture_factory():
    systems: list[FixtureSystem] = []

    def start(**overrides) -> FixtureSystem:
        root = tempfile.mkdtemp(prefix="9rs-")
        system = FixtureSystem(root=root, **overrides)
        systems.append(system)
        system.start()
        return system

    yield start
    for system in systems:
        system.stop()


@pytest.fixture
def fixture_system(fixture_factory) -> FixtureSystem:
    return fixture_factory()
