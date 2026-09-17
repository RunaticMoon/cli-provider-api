"""Start/stop the complete mock system: a real Runner subprocess plus a real API
subprocess talking over a real Unix socket and a real HTTP port.

No test calls a real CLI/account; the Runner loads only the synthetic mock driver.
"""

from __future__ import annotations

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

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


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


@dataclass
class MockSystem:
    root: str
    behavior: str = "success"
    api_key: str = "local-alpha-key"
    beta_key: str = "local-beta-key"
    gamma_key: str = "local-gamma-key"
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
                    "driver_id": "mock",
                    "driver_version": "0.1.0",
                    "distribution": "cli-driver-mock",
                    "socket_path": self.socket_path,
                    "connect_timeout_seconds": 5.0,
                }
            ],
            "presets": [
                {
                    "alias": "mock/text",
                    "runner_ref": "runner-1",
                    "model_id": "mock-model",
                    "allow_synthetic_unverified": True,
                },
                {
                    "alias": "mock/text-beta",
                    "runner_ref": "runner-1",
                    "model_id": "mock-model",
                    "allow_synthetic_unverified": True,
                },
                {
                    "alias": "mock/review",
                    "runner_ref": "runner-1",
                    "model_id": "mock-model",
                    "task_policy": "review",
                    "allow_synthetic_unverified": True,
                },
            ],
            "workspaces": [
                {"workspace_id": "ws-alpha"},
                {"workspace_id": "ws-beta"},
            ],
            "principals": [
                {
                    "name": "alpha",
                    "key_hash": hash_api_key(self.api_key),
                    "allowed_presets": ["mock/text"],
                    "allowed_workspaces": ["ws-alpha"],
                    "max_concurrency": 2,
                },
                {
                    "name": "beta",
                    "key_hash": hash_api_key(self.beta_key),
                    "allowed_presets": ["mock/review"],
                    "allowed_workspaces": ["ws-beta"],
                    "max_concurrency": 2,
                },
                {
                    "name": "gamma",
                    "key_hash": hash_api_key(self.gamma_key),
                    "allowed_presets": ["mock/text", "mock/text-beta", "mock/review"],
                    "allowed_workspaces": ["ws-alpha"],
                    "max_concurrency": 2,
                },
            ],
        }
        _merge(config, self.config_overrides)
        return config

    # ------------------------------------------------------------- lifecycle

    def start(self) -> "MockSystem":
        self.port = _free_port()
        self._config_path = os.path.join(self.root, "config.yaml")
        with open(self._config_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(self._config(), handle)

        env = os.environ.copy()
        env["CLI_DRIVER_MOCK_BEHAVIOR"] = self.behavior

        self.runner_proc = subprocess.Popen(
            [
                sys.executable, "-m", "cli_provider_runner", "serve",
                "--socket", self.socket_path,
                "--instance-id", "runner-1",
                "--driver-id", "mock",
                "--distribution", "cli-driver-mock",
                "--version", "0.1.0",
            ],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self._wait_for_socket()

        self._log = open(os.path.join(self.root, "api.log"), "w", encoding="utf-8")
        self.api_proc = subprocess.Popen(
            [
                sys.executable, "-m", "cli_provider_api", "serve",
                "--config", self._config_path,
                "--host", "127.0.0.1", "--port", str(self.port),
            ],
            cwd=REPO_ROOT, env=env,
            stdout=self._log, stderr=subprocess.STDOUT, text=True,
        )
        self._wait_for_ready()
        return self

    def kill_api(self) -> None:
        if self.api_proc is not None and self.api_proc.poll() is None:
            self.api_proc.kill()
            self.api_proc.wait(timeout=10)

    def restart_api(self) -> None:
        """Restart the API against the same DB/config (runner keeps running)."""
        self.kill_api()
        env = os.environ.copy()
        env["CLI_DRIVER_MOCK_BEHAVIOR"] = self.behavior
        self._log = open(os.path.join(self.root, "api.log"), "a", encoding="utf-8")
        self.api_proc = subprocess.Popen(
            [
                sys.executable, "-m", "cli_provider_api", "serve",
                "--config", self._config_path,
                "--host", "127.0.0.1", "--port", str(self.port),
            ],
            cwd=REPO_ROOT, env=env,
            stdout=self._log, stderr=subprocess.STDOUT, text=True,
        )
        self._wait_for_ready()

    def _wait_for_socket(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.runner_proc is not None and self.runner_proc.poll() is not None:
                err = self.runner_proc.stderr.read() if self.runner_proc.stderr else ""
                raise RuntimeError(f"runner exited early: {err.strip()}")
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
        raise TimeoutError("runner socket never became ready")

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
def system_factory():
    systems: list[MockSystem] = []

    def start(behavior: str = "success", **overrides) -> MockSystem:
        root = tempfile.mkdtemp(prefix="m1b-")
        system = MockSystem(root=root, behavior=behavior, **overrides)
        systems.append(system)
        system.start()
        return system

    yield start
    for system in systems:
        system.stop()


@pytest.fixture
def system(system_factory) -> MockSystem:
    return system_factory("success")


@pytest.fixture
def run_failure_system(system_factory) -> MockSystem:
    return system_factory("failed")


@pytest.fixture
def unknown_system(system_factory) -> MockSystem:
    return system_factory(
        "hang_ignores_cancel",
        config_overrides={
            "api": {
                "default_run_deadline_seconds": 0.3,
                "max_run_deadline_seconds": 0.5,
                "cancel_deadline_seconds": 0.5,
            }
        },
    )


@pytest.fixture
def hang_system(system_factory) -> MockSystem:
    return system_factory(
        "hang",
        config_overrides={
            "api": {
                "default_run_deadline_seconds": 0.4,
                "max_run_deadline_seconds": 0.6,
                "cancel_deadline_seconds": 0.5,
            }
        },
    )


@pytest.fixture
def queue_system(system_factory) -> MockSystem:
    """One runner slot and a long deadline so a second run stays queued."""
    return system_factory(
        "hang",
        config_overrides={
            "api": {
                "default_run_deadline_seconds": 30.0,
                "max_run_deadline_seconds": 60.0,
                "cancel_deadline_seconds": 2.0,
                "concurrency": {
                    "per_runner": 1,
                    "per_principal": 1,
                    "queue_timeout_seconds": 10.0,
                },
            }
        },
    )


@pytest.fixture
def long_hang_system(system_factory) -> MockSystem:
    return system_factory(
        "hang",
        config_overrides={
            "api": {
                "default_run_deadline_seconds": 30.0,
                "max_run_deadline_seconds": 60.0,
                "cancel_deadline_seconds": 2.0,
            }
        },
    )
