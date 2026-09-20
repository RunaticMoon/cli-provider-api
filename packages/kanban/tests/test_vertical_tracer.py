"""REAL vertical tracer — the honest end-to-end, no fakes on the wire.

An actual Hermes board (bridge subprocess) + the real ``WrapperClient`` +
a real ``cli-provider-api`` subprocess + a real ``cli-provider-runner``
subprocess (synthetic mock driver) bound by the real operator execution
config to the operator-prepared worktree:

    Hermes card -> kernel claim -> admission (prepared worktree + Runner
    binding) -> real POST /v1/chat/completions -> API -> UDS -> Runner ->
    mock driver -> run view -> strict status/outcome + context echo ->
    trusted verification -> evidence -> fenced review handoff.

The mock driver is the synthetic lane: it writes nothing into the
worktree, so the run view's ``synthetic`` flag is asserted rather than
hidden. The parent runs the native canary after merge.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from cli_provider_core import hash_api_key
from cli_provider_kanban.dispatch import dispatch_once
from cli_provider_kanban.store import DispatchStore

from conftest import (  # noqa: F401
    dispatch_policy,
    requires_hermes,
    write_policy,
)
from test_dispatch import HERMES_ENV_KEYS, _spec_for, board  # noqa: F401

pytestmark = requires_hermes

REPO_ROOT = Path(__file__).resolve().parents[3]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(proc, predicate, timeout, label, log=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            err = ""
            if proc.stderr:
                err = proc.stderr.read()
            if log:
                err += "\n" + Path(log).read_text(errors="replace")
            raise RuntimeError(f"{label} exited early: {err.strip()[:2000]}")
        try:
            if predicate():
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise TimeoutError(f"{label} never became ready")


@pytest.fixture
def real_stack(tmp_path, dispatch_policy):
    """Real Runner subprocess (mock driver, bound by the real execution
    config) + real API subprocess on an ephemeral port."""
    data, rev = dispatch_policy
    ws = data["workspaces"]["ws-main"]
    exec_config = ws["runner_execution_config"]  # written 0600, 0700 parent
    socket_path = str(tmp_path / "runner.sock")
    api_key = "local-alpha-key"
    key_file = tmp_path / "api.key"
    key_file.write_text(api_key + "\n")
    os.chmod(key_file, 0o600)

    port = _free_port()
    config = {
        "schema_version": 1,
        "data_dir": str(tmp_path / "data"),
        "api": {
            "host": "127.0.0.1",
            "port": port,
            "default_run_deadline_seconds": 10.0,
            "max_run_deadline_seconds": 20.0,
            "cancel_deadline_seconds": 1.0,
            "keepalive_seconds": 0.3,
            "concurrency": {"per_runner": 1, "per_principal": 2,
                            "queue_timeout_seconds": 2.0},
        },
        "runners": [{
            "instance_id": "runner-1",
            "driver_id": "mock",
            "driver_version": "0.1.0",
            "distribution": "cli-driver-mock",
            "socket_path": socket_path,
            "connect_timeout_seconds": 5.0,
        }],
        "presets": [{
            "alias": "mock/text",
            "runner_ref": "runner-1",
            "model_id": "mock-model",
            "allow_synthetic_unverified": True,
        }],
        "workspaces": [{"workspace_id": "ws-alpha"}],
        "principals": [{
            "name": "alpha",
            "key_hash": hash_api_key(api_key),
            "allowed_presets": ["mock/text"],
            "allowed_workspaces": ["ws-alpha"],
            "max_concurrency": 2,
        }],
    }
    config_path = tmp_path / "api-config.yaml"
    config_path.write_text(yaml.safe_dump(config))

    env = dict(os.environ, CLI_DRIVER_MOCK_BEHAVIOR="success")
    runner_log = tmp_path / "runner.log"
    runner = subprocess.Popen(
        [
            sys.executable, "-m", "cli_provider_runner", "serve",
            "--socket", socket_path,
            "--instance-id", "runner-1",
            "--driver-id", "mock",
            "--distribution", "cli-driver-mock",
            "--version", "0.1.0",
            "--execution-config", exec_config,
        ],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=runner_log.open("w"),
    )
    _wait_for(runner, lambda: Path(socket_path).exists(), 20.0, "runner",
              log=str(runner_log))

    api_log = tmp_path / "api.log"
    api = subprocess.Popen(
        [
            sys.executable, "-m", "cli_provider_api", "serve",
            "--config", str(config_path),
            "--host", "127.0.0.1", "--port", str(port),
        ],
        cwd=REPO_ROOT, env=env,
        stdout=api_log.open("w"), stderr=subprocess.STDOUT,
    )

    import urllib.request

    def _ready():
        base = f"http://127.0.0.1:{port}"
        for probe in ("/health/live", "/health/ready"):
            with urllib.request.urlopen(base + probe, timeout=2) as resp:
                if resp.status != 200:
                    return False
        return True

    _wait_for(api, _ready, 30.0, "api", log=str(api_log))
    yield {
        "base_url": f"http://127.0.0.1:{port}",
        "key_file": str(key_file),
        "socket_path": socket_path,
    }
    api.terminate()
    runner.terminate()
    for proc, name in ((api, "api"), (runner, "runner")):
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_dispatch_real_api_runner_prepared_worktree(
    board, tmp_path, dispatch_policy, real_stack
):
    """The genuine path: real board, real HTTP submit, real Runner binding,
    real verification, fenced review handoff — with the run flagged
    synthetic (mock driver writes nothing into the worktree)."""
    db, bridge = board
    data, rev = dispatch_policy
    ws = data["workspaces"]["ws-main"]
    prepared = Path(ws["prepared_worktree"])

    data["execution"] = {
        "mode": "direct",
        "base_url": real_stack["base_url"],
        "credential_file": real_stack["key_file"],
        "model": "mock/text",
    }
    tid = bridge.call("create_task", title="tracer", assignee="jev-native",
                      body="x")["task_id"]
    spec = _spec_for(tid, rev)
    del spec["task_id"]
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(tm)
    policy_path = write_policy(tmp_path, data)
    store_path = tmp_path / "dispatch.db"

    # client=None -> the real WrapperClient against the real API.
    report = dispatch_once(board_db=db, policy_path=policy_path,
                           store_path=store_path)
    rec = next(r for r in report["results"] if r["task_id"] == tid)
    assert rec["action"] == "review", rec

    receipt = DispatchStore(store_path).for_task(tid)[0]
    assert receipt.state == "review"
    assert receipt.worktree == str(prepared)
    assert receipt.run_id

    # The run really executed under the Runner's binding — the worktree
    # carries the runner's lock file as proof it was claimed as a root.
    assert (prepared / ".cli-provider-runner.lock").exists()

    # The card is in review, never done.
    assert bridge.call("get_task", task_id=tid)["task"]["status"] == "review"

    # The mock lane is honestly synthetic — asserted, not hidden.
    from cli_provider_kanban.wrapper_client import WrapperClient
    run = WrapperClient(real_stack["base_url"],
                        credential_file=real_stack["key_file"]
                        ).get_run(receipt.run_id)
    assert run["status"] == "completed"
    assert run["outcome"] == "succeeded"
    assert run["synthetic"] is True
    assert run["task_id"] == tid
    assert run["workspace_id"] == "ws-alpha"
    assert run["execution"]["task_revision"] == str(spec["task_revision"])
    assert run["execution"]["base_revision"] == rev
    assert run["execution"]["route"] == "worker.code.standard"

    # Durable sanitized evidence landed on the receipt.
    assert receipt.evidence["verification"]["exit_code"] == 0
    assert receipt.evidence["diff"]["sha256"]
