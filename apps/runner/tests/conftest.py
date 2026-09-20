"""Spawns REAL runner subprocesses for boundary tests.

Every test in this directory talks to a real `cli-provider-runner serve`
subprocess over a private Unix-domain socket using the JSON protocol. No test
starts a real provider CLI or touches any account/auth material.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


class RunnerProcess:
    def __init__(self, socket_path: str, proc: subprocess.Popen[str]) -> None:
        self.socket_path = socket_path
        self.proc = proc

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def wait_exit(self, timeout: float = 10.0) -> tuple[int, str, str]:
        out, err = self.proc.communicate(timeout=timeout)
        return self.proc.returncode, out, err


def _wait_for_socket(path: str, proc: subprocess.Popen[str], timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read() if proc.stderr else ""
            raise RuntimeError(
                f"runner exited early rc={proc.returncode}: {err.strip()}"
            )
        if os.path.exists(path):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(1.0)
            try:
                probe.connect(path)
                return
            except OSError:
                pass
            finally:
                probe.close()
        time.sleep(0.05)
    raise TimeoutError(f"runner socket {path} was never ready")


@pytest.fixture
def sock_dir():
    directory = tempfile.mkdtemp(prefix="m1a-")
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def runner_factory(sock_dir):
    spawned: list[RunnerProcess] = []
    counter = {"n": 0}

    def start(
        behavior: str = "success",
        *,
        driver_id: str = "mock",
        distribution: str = "cli-driver-mock",
        version: str = "0.1.0",
        malformed_mode: str | None = None,
        max_frame_bytes: int | None = None,
        max_queue: int | None = None,
        cancel_deadline: float | None = None,
        cancel_detail: str | None = None,
        umask: int | None = None,
        extra_env: dict[str, str] | None = None,
        execution_config: str | None = None,
        wait: bool = True,
    ) -> RunnerProcess:
        socket_path = os.path.join(sock_dir, f"r{counter['n']}.sock")
        counter["n"] += 1
        env = os.environ.copy()
        env["CLI_DRIVER_MOCK_BEHAVIOR"] = behavior
        env.update(extra_env or {})
        if malformed_mode is not None:
            env["CLI_DRIVER_MOCK_MALFORMED_MODE"] = malformed_mode
        if cancel_detail is not None:
            env["CLI_DRIVER_MOCK_CANCEL_DETAIL"] = cancel_detail
        cmd = [
            sys.executable,
            "-m",
            "cli_provider_runner",
            "serve",
            "--socket",
            socket_path,
            "--instance-id",
            "inst-test",
            "--driver-id",
            driver_id,
            "--distribution",
            distribution,
            "--version",
            version,
        ]
        if max_frame_bytes is not None:
            cmd += ["--max-frame-bytes", str(max_frame_bytes)]
        if max_queue is not None:
            cmd += ["--max-queue", str(max_queue)]
        if cancel_deadline is not None:
            cmd += ["--cancel-deadline", str(cancel_deadline)]
        if execution_config is not None:
            cmd += ["--execution-config", str(execution_config)]
        preexec_fn = None
        if umask is not None:
            preexec_fn = lambda: os.umask(umask)  # noqa: E731 - POSIX only
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=preexec_fn,
        )
        runner = RunnerProcess(socket_path, proc)
        spawned.append(runner)
        if wait:
            _wait_for_socket(socket_path, proc)
        return runner

    yield start
    for runner in spawned:
        runner.stop()


def run_params(run_id: str = "run-1", **overrides) -> dict:
    params = {
        "run_id": run_id,
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "preset": "mock/text",
        "workspace": {"workspace_id": "ws-1"},
        "messages": [{"role": "user", "content": "hello"}],
    }
    params.update(overrides)
    return params
