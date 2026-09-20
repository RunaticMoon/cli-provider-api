"""End-to-end wiring: real Runner subprocess -> allowlisted Devin driver -> fixture.

The Devin driver package is allowlisted like any other distribution; these
tests prove the standalone Runner/UDS path can spawn the ACP subprocess through
the injected executor and stream its events - all against the checked-in
``fake_devin`` fixture, never a real CLI or account.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from cli_provider_runner.client import RunnerClient

from conftest import run_params

DRIVER_FIXTURE = (
    Path(__file__).parent.parent.parent.parent
    / "drivers"
    / "devin"
    / "tests"
    / "fixtures"
    / "fake_devin.py"
)


def devin_env(tmp_path: Path, mode: str = "ok", catalog: str = "ok") -> dict[str, str]:
    """Runner-process env that points the Devin driver at the fixture.

    ``DEVIN_REFUSAL_FALLBACK`` is deliberately set so the test proves the driver
    strips it before the child CLI ever sees it.
    """
    wrapper = tmp_path / "devin"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{DRIVER_FIXTURE}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return {
        "DEVIN_CLI": str(wrapper),
        "DEVIN_WORKSPACE_ROOT": str(tmp_path),
        "DEVIN_REFUSAL_FALLBACK": "claude-opus-5-high",
        "DEVIN_MODEL": "swe-2-max",
        "FAKE_DEVIN_MODE": mode,
        "FAKE_DEVIN_CATALOG": catalog,
        "FAKE_DEVIN_LOG": str(tmp_path / "fixture.log"),
    }


def write_execution_config(tmp_path: Path, *, actions: list[str] | None = None) -> str:
    """Operator-side binding: workspace id ``ws-1`` -> the test root.

    The Devin driver is non-synthetic, so a real Runner refuses to run it
    without an execution config binding the request's workspace_id. The file
    lives in a private 0700 dir with 0600 permissions, exactly as the loader
    requires.
    """
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
                        "root": str(tmp_path),
                        "allowed_actions": list(actions or []),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    conf.chmod(0o600)
    return str(conf)


def read_log(tmp_path: Path) -> list[dict]:
    logfile = tmp_path / "fixture.log"
    if not logfile.exists():
        return []
    return [json.loads(line) for line in logfile.read_text().splitlines() if line.strip()]


async def drive(client: RunnerClient, params: dict) -> list:
    return [envelope async for envelope in client.run(params)]


async def test_runner_loads_devin_driver_and_runs_over_uds(runner_factory, tmp_path):
    runner = runner_factory(
        "success",
        driver_id="devin",
        distribution="cli-driver-devin",
        version="0.1.0",
        extra_env=devin_env(tmp_path),
        execution_config=write_execution_config(tmp_path),
    )
    client = await RunnerClient.connect(runner.socket_path)
    try:
        manifest = await client.call("manifest")
        assert manifest.ok
        assert manifest.result["driver_id"] == "devin"
        assert manifest.result["protocol_family"] == "acp"
        assert manifest.result["supported_transports"] == ["acp"]

        # Probe exercises --version + a bounded ACP initialize handshake.
        probe = await client.call("probe")
        assert probe.ok and probe.result["ok"] is True
        assert probe.result["cli_version"] == "3000.10.31"

        models = await client.call("discover_models")
        assert models.ok
        descriptor = models.result["models"][0]
        assert descriptor["model_id"] == "swe-2-max"
        assert descriptor["verification"]["status"] == "passed"

        params = run_params(
            preset="devin/code", model_alias="swe-2-max", deadline_seconds=15.0
        )
        events = await drive(client, params)
        response = client.last_run_response
        assert response is not None and response.ok
        result = response.result
        assert result["status"] == "completed"
        assert result["outcome"] == "succeeded"
        assert result["verification"]["status"] == "not_run"
        assert result["usage"]["provenance"] == "unknown"
        assert result["synthetic"] is False

        kinds = [envelope.event.kind for envelope in events]
        assert kinds[0] == "run.started"
        assert kinds[-1] == "run.completed"
        assert "message.delta" in kinds
        text = "".join(
            envelope.event.payload.text
            for envelope in events
            if envelope.event.kind == "message.delta"
        )
        assert text == "Hello world"
        serialized = json.dumps([e.event.model_dump(mode="json") for e in events])
        assert "SECRET-THOUGHT-TEXT" not in serialized

        # The fixture's env log proves the sanitized child never saw the
        # inherited DEVIN_* overrides.
        records = read_log(tmp_path)
        env_records = [r["env"] for r in records if r.get("event") == "acp_start"]
        assert env_records
        assert env_records[0]["DEVIN_REFUSAL_FALLBACK"] == "absent"
        seen = [r.get("received") for r in records]
        assert "session/prompt:begin" in seen
    finally:
        await client.aclose()


async def test_runner_reports_devin_failure_not_success(runner_factory, tmp_path):
    runner = runner_factory(
        "success",
        driver_id="devin",
        distribution="cli-driver-devin",
        version="0.1.0",
        extra_env=devin_env(tmp_path, mode="model_mismatch"),
        execution_config=write_execution_config(tmp_path),
    )
    client = await RunnerClient.connect(runner.socket_path)
    try:
        await drive(
            client,
            run_params(preset="devin/code", model_alias="swe-2-max", deadline_seconds=15.0),
        )
        result = client.last_run_response.result
        assert result["status"] == "failed"
        assert result["outcome"] == "provider_error"
        # The model mismatch is caught before any prompt reaches the fixture.
        seen = [r.get("received") for r in read_log(tmp_path)]
        assert "session/prompt:begin" not in seen
    finally:
        await client.aclose()


async def test_runner_deadline_cancels_hung_devin_run(runner_factory, tmp_path):
    runner = runner_factory(
        "success",
        driver_id="devin",
        distribution="cli-driver-devin",
        version="0.1.0",
        extra_env=devin_env(tmp_path, mode="hang"),
        cancel_deadline=2.0,
        execution_config=write_execution_config(tmp_path),
    )
    client = await RunnerClient.connect(runner.socket_path)
    try:
        await drive(client, run_params(deadline_seconds=0.5))
        result = client.last_run_response.result
        assert result["status"] == "cancelled"
        assert result["outcome"] == "cancelled"
        # No orphaned fixture process: the driver kills the process group.
        await asyncio.sleep(0.2)
        records = read_log(tmp_path)
        assert any(r.get("event") == "acp_start" for r in records)
    finally:
        await client.aclose()


async def test_unbound_workspace_is_denied_before_any_spawn(
    runner_factory, tmp_path
):
    # ws-9 is not in the operator config: the run must be refused before the
    # fixture CLI (or any driver code) executes — nothing may create a task
    # home or spawn a process for it.
    runner = runner_factory(
        "success",
        driver_id="devin",
        distribution="cli-driver-devin",
        version="0.1.0",
        extra_env=devin_env(tmp_path),
        execution_config=write_execution_config(tmp_path),
    )
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client,
            run_params(
                workspace={"workspace_id": "ws-9"}, deadline_seconds=15.0
            ),
        )
        result = client.last_run_response.result
        assert result["status"] == "failed"
        assert events[-1].event.payload.code == "workspace_not_bound"
        assert read_log(tmp_path) == []
    finally:
        await client.aclose()


async def test_devin_without_execution_config_is_denied(runner_factory, tmp_path):
    # A real (non-synthetic) driver must never run an unbound workspace, even
    # though the operator env still supplies a static DEVIN_WORKSPACE_ROOT.
    runner = runner_factory(
        "success",
        driver_id="devin",
        distribution="cli-driver-devin",
        version="0.1.0",
        extra_env=devin_env(tmp_path),
    )
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(client, run_params(deadline_seconds=15.0))
        result = client.last_run_response.result
        assert result["status"] == "failed"
        assert events[-1].event.payload.code == "execution_config_required"
        assert read_log(tmp_path) == []
    finally:
        await client.aclose()
