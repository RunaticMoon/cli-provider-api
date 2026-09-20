"""Operator execution-config binding: protected file, UDS deny/allow, serial claim.

Every socket test talks to a real ``cli-provider-runner serve`` subprocess (or
the in-process server over a real UDS) — the binding path is exercised exactly
as production uses it. The only workspace roots involved are test-owned tmp
dirs; no provider CLI or account is touched.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from cli_provider_runner.client import RunnerClient
from cli_provider_runner.execution_config import (
    BoundPermissions,
    BoundWorkspace,
    ExecutionConfigError,
    load_execution_config,
)

from conftest import run_params

GOOD_ROOT_NAME = "ws-root"


def write_config(
    tmp_path: Path,
    workspaces: dict,
    *,
    file_mode: int = 0o600,
    dir_mode: int = 0o700,
) -> Path:
    """Write an execution config in a protected location."""
    conf_dir = tmp_path / "protected"
    conf_dir.mkdir(mode=dir_mode, exist_ok=True)
    os.chmod(conf_dir, dir_mode)
    conf = conf_dir / "execution.json"
    conf.write_text(
        json.dumps({"version": 1, "workspaces": workspaces}), encoding="utf-8"
    )
    os.chmod(conf, file_mode)
    return conf


def make_root(tmp_path: Path, name: str = GOOD_ROOT_NAME) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return root


# ----------------------------------------------------------- file validation


def test_loads_a_valid_config(tmp_path):
    root = make_root(tmp_path)
    conf = write_config(
        tmp_path,
        {"ws-1": {"root": str(root), "allowed_actions": ["hermes.yolo"]}},
    )
    config = load_execution_config(str(conf))
    binding = config.binding_for("ws-1")
    assert binding is not None
    assert binding.root == str(root)
    assert binding.allowed_actions == ["hermes.yolo"]
    assert config.binding_for("ws-other") is None


def test_group_readable_config_is_accepted_and_documented(tmp_path):
    root = make_root(tmp_path)
    conf = write_config(tmp_path, {"ws-1": {"root": str(root)}}, file_mode=0o640)
    assert load_execution_config(str(conf)).binding_for("ws-1") is not None


@pytest.mark.parametrize("mode", [0o644, 0o664, 0o620, 0o606, 0o700])
def test_rejects_file_modes_beyond_owner_rw_group_read(tmp_path, mode):
    root = make_root(tmp_path)
    conf = write_config(
        tmp_path, {"ws-1": {"root": str(root)}}, file_mode=mode
    )
    with pytest.raises(ExecutionConfigError) as exc:
        load_execution_config(str(conf))
    assert exc.value.code == "CONFIG_PERMISSIONS"


def test_rejects_a_non_private_parent_directory(tmp_path):
    root = make_root(tmp_path)
    conf = write_config(
        tmp_path, {"ws-1": {"root": str(root)}}, dir_mode=0o750
    )
    with pytest.raises(ExecutionConfigError) as exc:
        load_execution_config(str(conf))
    assert exc.value.code == "CONFIG_PERMISSIONS"


def test_rejects_a_symlinked_config_file(tmp_path):
    root = make_root(tmp_path)
    conf = write_config(tmp_path, {"ws-1": {"root": str(root)}})
    link = tmp_path / "protected" / "linked.json"
    link.symlink_to(conf)
    with pytest.raises(ExecutionConfigError) as exc:
        load_execution_config(str(link))
    assert exc.value.code == "CONFIG_UNTRUSTED"


def test_rejects_a_symlinked_parent_directory(tmp_path):
    root = make_root(tmp_path)
    conf = write_config(tmp_path, {"ws-1": {"root": str(root)}})
    alias = tmp_path / "alias"
    alias.symlink_to(conf.parent, target_is_directory=True)
    with pytest.raises(ExecutionConfigError) as exc:
        load_execution_config(str(alias / "execution.json"))
    assert exc.value.code == "CONFIG_UNTRUSTED"


def test_missing_config_fails_closed(tmp_path):
    with pytest.raises(ExecutionConfigError) as exc:
        load_execution_config(str(tmp_path / "protected" / "absent.json"))
    assert exc.value.code in ("CONFIG_UNTRUSTED", "CONFIG_UNREADABLE")


def test_non_regular_file_is_rejected(tmp_path):
    conf_dir = tmp_path / "protected"
    conf_dir.mkdir(mode=0o700)
    fifo = conf_dir / "execution.json"
    os.mkfifo(fifo)
    with pytest.raises(ExecutionConfigError) as exc:
        load_execution_config(str(fifo))
    assert exc.value.code == "CONFIG_UNTRUSTED"


def test_malformed_json_is_rejected(tmp_path):
    conf_dir = tmp_path / "protected"
    conf_dir.mkdir(mode=0o700)
    conf = conf_dir / "execution.json"
    conf.write_text("{not json", encoding="utf-8")
    os.chmod(conf, 0o600)
    with pytest.raises(ExecutionConfigError) as exc:
        load_execution_config(str(conf))
    assert exc.value.code == "CONFIG_INVALID"


def test_extra_keys_are_rejected(tmp_path):
    root = make_root(tmp_path)
    conf = write_config(
        tmp_path,
        {"ws-1": {"root": str(root), "argv": ["rm", "-rf", "/"]}},
    )
    with pytest.raises(ExecutionConfigError) as exc:
        load_execution_config(str(conf))
    assert exc.value.code == "CONFIG_INVALID"


@pytest.mark.parametrize(
    "binding",
    [
        {"root": "relative/dir"},
        {"root": "/definitely/absent/cli-provider-test-root"},
        {"root": str(Path("/tmp")), "allowed_actions": ["shell.exec"]},
        {"root": str(Path("/tmp")), "allowed_actions": ["hermes.yolo", "hermes.yolo"]},
    ],
    ids=["relative", "missing-root", "unknown-action", "duplicate-action"],
)
def test_invalid_bindings_are_rejected(tmp_path, binding):
    conf = write_config(tmp_path, {"ws-1": binding})
    with pytest.raises(ExecutionConfigError) as exc:
        load_execution_config(str(conf))
    assert exc.value.code == "CONFIG_INVALID"


def test_symlinked_workspace_root_is_rejected(tmp_path):
    real = make_root(tmp_path, "real-root")
    link = tmp_path / "link-root"
    link.symlink_to(real, target_is_directory=True)
    conf = write_config(tmp_path, {"ws-1": {"root": str(link)}})
    with pytest.raises(ExecutionConfigError) as exc:
        load_execution_config(str(conf))
    assert exc.value.code == "CONFIG_INVALID"


def test_unknown_workspace_id_in_config_keys_is_rejected(tmp_path):
    root = make_root(tmp_path)
    conf = write_config(tmp_path, {"bad id!": {"root": str(root)}})
    with pytest.raises(ExecutionConfigError) as exc:
        load_execution_config(str(conf))
    assert exc.value.code == "CONFIG_INVALID"


# -------------------------------------------------------------- bound objects


def test_bound_workspace_resolve_rules(tmp_path):
    root = make_root(tmp_path)
    outside = make_root(tmp_path, "outside")
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    ws = BoundWorkspace("ws-1", str(root))
    assert ws.root == str(root)
    assert ws.resolve("sub/file.txt") == os.path.join(str(root), "sub/file.txt")
    for bad in (
        "/etc/passwd",
        "../outside/secret.txt",
        "sub/../../escape",
        "escape/secret.txt",  # symlink inside the root pointing outside it
        "",
    ):
        with pytest.raises(ValueError):
            ws.resolve(bad)


def test_bound_permissions_are_deny_by_default():
    policy = BoundPermissions("ws-1", ["hermes.yolo"])
    assert policy.allows("hermes.yolo") is True
    assert policy.allows("devin.acp.session_mode.bypass") is False
    assert policy.allows("anything.else") is False
    empty = BoundPermissions("ws-1", [])
    assert empty.allows("hermes.yolo") is False


# ------------------------------------------------------------- UDS behaviour


async def drive(client: RunnerClient, params: dict) -> list:
    return [envelope async for envelope in client.run(params)]


async def test_bound_workspace_run_completes_over_uds(runner_factory, tmp_path):
    root = make_root(tmp_path)
    conf = write_config(tmp_path, {"ws-1": {"root": str(root)}})
    runner = runner_factory("success", execution_config=str(conf))
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(client, run_params())
        response = client.last_run_response
        assert response.ok
        assert response.result["status"] == "completed"
        assert events[-1].event.kind == "run.completed"
        # The bound workspace is claimed for the run's lifetime.
        assert (root / ".cli-provider-runner.lock").exists()
    finally:
        await client.aclose()


async def test_unbound_workspace_id_is_denied_before_the_driver(
    runner_factory, tmp_path
):
    root = make_root(tmp_path)
    conf = write_config(tmp_path, {"ws-1": {"root": str(root)}})
    runner = runner_factory("success", execution_config=str(conf))
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client, run_params(workspace={"workspace_id": "ws-unknown"})
        )
        response = client.last_run_response
        assert response.ok
        assert response.result["status"] == "failed"
        assert response.result["terminal_kind"] == "run.failed"
        assert events[-1].event.kind == "run.failed"
        assert events[-1].event.payload.code == "workspace_not_bound"
    finally:
        await client.aclose()


async def test_config_with_empty_workspaces_denies_everything(
    runner_factory, tmp_path
):
    conf = write_config(tmp_path, {})
    runner = runner_factory("success", execution_config=str(conf))
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(client, run_params())
        assert events[-1].event.payload.code == "workspace_not_bound"
        assert client.last_run_response.result["status"] == "failed"
    finally:
        await client.aclose()


async def test_preset_and_model_pins_are_enforced(runner_factory, tmp_path):
    root = make_root(tmp_path)
    conf = write_config(
        tmp_path,
        {
            "ws-1": {
                "root": str(root),
                "allowed_presets": ["mock/text"],
                "allowed_models": ["mock-model"],
            }
        },
    )
    runner = runner_factory("success", execution_config=str(conf))
    client = await RunnerClient.connect(runner.socket_path)
    try:
        allowed = await drive(
            client, run_params(model_alias="mock-model")
        )
        assert client.last_run_response.result["status"] == "completed"
        assert allowed[-1].event.kind == "run.completed"

        await drive(client, run_params(run_id="run-2", preset="other/preset"))
        assert client.last_run_response.result["status"] == "failed"
        events = await drive(
            client,
            run_params(run_id="run-3", model_alias="other-model"),
        )
        assert events[-1].event.payload.code == "model_not_allowed"
    finally:
        await client.aclose()


async def test_unconfigured_runner_keeps_legacy_mock_behaviour(runner_factory):
    runner = runner_factory("success")
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(client, run_params())
        assert client.last_run_response.result["status"] == "completed"
        assert events[-1].event.kind == "run.completed"
    finally:
        await client.aclose()


async def test_bound_workspace_is_claimed_serially_across_runners(
    runner_factory, tmp_path
):
    root = make_root(tmp_path)
    conf = write_config(tmp_path, {"ws-1": {"root": str(root)}})
    first = runner_factory("hang", execution_config=str(conf))
    second = runner_factory("success", execution_config=str(conf))
    client1 = await RunnerClient.connect(first.socket_path)
    client2 = await RunnerClient.connect(second.socket_path)
    canceller = await RunnerClient.connect(first.socket_path)
    try:
        task = asyncio.create_task(
            drive(client1, run_params(deadline_seconds=30.0))
        )
        await asyncio.sleep(0.4)
        events = await drive(client2, run_params(run_id="run-2"))
        assert client2.last_run_response.result["status"] == "failed"
        assert events[-1].event.payload.code == "workspace_busy"
        cancel = await canceller.call("cancel", {"run_id": "run-1"})
        assert cancel.result["requested"] is True
        await asyncio.wait_for(task, timeout=10)
    finally:
        await client1.aclose()
        await client2.aclose()
        await canceller.aclose()


async def test_cli_serve_refuses_an_untrusted_execution_config(
    runner_factory, tmp_path
):
    conf_dir = tmp_path / "protected"
    conf_dir.mkdir(mode=0o700)
    conf = conf_dir / "execution.json"
    conf.write_text("{not json", encoding="utf-8")
    os.chmod(conf, 0o600)
    runner = runner_factory("success", execution_config=str(conf), wait=False)
    rc, _out, err = runner.wait_exit(timeout=15)
    assert rc == 2
    assert "CONFIG_INVALID" in err


def test_serve_help_lists_execution_config():
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "cli_provider_runner", "serve", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert "--execution-config" in proc.stdout
