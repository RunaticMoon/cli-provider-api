"""End-to-end wiring: real Runner subprocess -> allowlisted Antigravity driver -> fixture.

The Antigravity driver package is allowlisted like any other distribution; these
tests prove the standalone Runner/UDS path spawns the synthetic ``agy`` stream-json
subprocess through the injected executor and streams its events - all against the
checked-in ``fake_agy`` fixture, never a real CLI or account.

Writable paths stay inside pytest ``tmp_path``/``sock_dir`` temp roots; no test
touches /etc, /opt, /var/lib or any real host path.
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

pytestmark = pytest.mark.anyio

DRIVER_FIXTURE = (
    Path(__file__).parent.parent.parent.parent
    / "drivers"
    / "antigravity"
    / "tests"
    / "fixtures"
    / "fake_agy.py"
)

MODEL = "gemini-3.8-flash-high"
MODEL_2 = "claude-opus-4-6-thinking"
PINNED_VERSION = "1.2.7"
SKIP_ACTION = "antigravity.dangerously_skip_permissions"


def agy_env(
    tmp_path: Path,
    mode: str = "ok",
    catalog: str = "ok",
    *,
    allow_skip: bool = False,
) -> dict[str, str]:
    """Runner-process env that points the Antigravity driver at the fixture.

    ``AGY_MODEL`` is deliberately set to a decoy so the test proves the driver
    neither reads nor forwards any singleton model env var to the child.
    """
    wrapper = tmp_path / "agy"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{DRIVER_FIXTURE}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    env = {
        "AGY_CLI": str(wrapper),
        "AGY_MODELS": f"{MODEL},{MODEL_2}",
        "AGY_EXPECTED_VERSION": PINNED_VERSION,
        "AGY_MODEL": "decoy-not-a-real-model",
        "FAKE_AGY_MODE": mode,
        "FAKE_AGY_CATALOG": catalog,
        "FAKE_AGY_LOG": str(tmp_path / "fixture.log"),
    }
    if allow_skip:
        env["AGY_ALLOW_SKIP_PERMISSIONS"] = "1"
    return env


def write_execution_config(
    tmp_path: Path,
    *,
    actions: list[str] | None = None,
    allowed_presets: list[str] | None = None,
    allowed_models: list[str] | None = None,
) -> str:
    """Operator-side binding: workspace id ``ws-1`` -> the test root."""
    conf_dir = tmp_path / "protected"
    conf_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(conf_dir, 0o700)
    binding: dict = {"root": str(tmp_path), "allowed_actions": list(actions or [])}
    if allowed_presets is not None:
        binding["allowed_presets"] = allowed_presets
    if allowed_models is not None:
        binding["allowed_models"] = allowed_models
    conf = conf_dir / "execution.json"
    conf.write_text(
        json.dumps({"version": 1, "workspaces": {"ws-1": binding}}),
        encoding="utf-8",
    )
    conf.chmod(0o600)
    return str(conf)


def read_log(tmp_path: Path) -> list[dict]:
    logfile = tmp_path / "fixture.log"
    if not logfile.exists():
        return []
    return [json.loads(line) for line in logfile.read_text().splitlines() if line.strip()]


def session_spawns(tmp_path: Path) -> list[dict]:
    return [
        record
        for record in read_log(tmp_path)
        if record.get("event") == "start"
        and "--version" not in record["argv"]
        and "models" not in record["argv"]
    ]


def agy_runner(runner_factory, tmp_path: Path, mode: str = "ok", **env_kwargs):
    def start(*, actions=None, config=True, **kwargs):
        return runner_factory(
            "success",
            driver_id="antigravity",
            distribution="cli-driver-antigravity",
            version="0.1.0",
            extra_env=agy_env(tmp_path, mode, **env_kwargs),
            execution_config=(
                write_execution_config(tmp_path, actions=actions) if config else None
            ),
            **kwargs,
        )

    return start


async def drive(client: RunnerClient, params: dict) -> list:
    return [envelope async for envelope in client.run(params)]


async def test_runner_loads_antigravity_driver_and_runs_over_uds(
    runner_factory, tmp_path
):
    runner = agy_runner(runner_factory, tmp_path)()
    client = await RunnerClient.connect(runner.socket_path)
    try:
        manifest = await client.call("manifest")
        assert manifest.ok
        assert manifest.result["driver_id"] == "antigravity"
        assert manifest.result["protocol_family"] == "antigravity-ndjson"
        assert manifest.result["synthetic"] is False

        probe = await client.call("probe")
        assert probe.ok and probe.result["ok"] is True
        assert probe.result["cli_version"] == PINNED_VERSION

        models = await client.call("discover_models")
        assert models.ok
        by_id = {d["model_id"]: d for d in models.result["models"]}
        assert by_id[MODEL]["verification"]["status"] == "passed"
        assert by_id[MODEL_2]["verification"]["status"] == "passed"

        params = run_params(
            preset=f"antigravity/{MODEL}",
            model_alias=MODEL,
            deadline_seconds=15.0,
        )
        events = await drive(client, params)
        response = client.last_run_response
        assert response is not None and response.ok
        result = response.result
        assert result["status"] == "completed"
        assert result["outcome"] == "succeeded"
        assert result["usage"]["provenance"] == "reported"
        assert result["synthetic"] is False

        kinds = [envelope.event.kind for envelope in events]
        assert kinds[0] == "run.started"
        assert kinds[-1] == "run.completed"
        text = "".join(
            envelope.event.payload.text
            for envelope in events
            if envelope.event.kind == "message.delta"
        )
        assert text == "hello world"
        serialized = json.dumps([e.event.model_dump(mode="json") for e in events])
        assert "hi\n" not in serialized  # tool output is never answer text

        # The spawned session is bound to the admitted model and carries no
        # permission bypass by default; operator env vars are scrubbed.
        spawns = session_spawns(tmp_path)
        assert len(spawns) == 1
        argv = spawns[0]["argv"]
        assert argv[argv.index("--model") + 1] == MODEL
        assert "--dangerously-skip-permissions" not in argv
        assert spawns[0]["agy_env"] == {}
        assert "--effort" not in argv
    finally:
        await client.aclose()


async def test_runner_runs_second_exact_preset(runner_factory, tmp_path):
    runner = agy_runner(runner_factory, tmp_path)()
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client,
            run_params(
                preset=f"antigravity/{MODEL_2}",
                model_alias=MODEL_2,
                deadline_seconds=15.0,
            ),
        )
        result = client.last_run_response.result
        assert result["status"] == "completed"
        argv = session_spawns(tmp_path)[0]["argv"]
        assert argv[argv.index("--model") + 1] == MODEL_2
    finally:
        await client.aclose()


async def test_runner_rejects_model_outside_allowlist(runner_factory, tmp_path):
    # gemini-3.8-flash-medium exists in the catalog but not in AGY_MODELS:
    # the driver must refuse before spawning a session.
    runner = agy_runner(runner_factory, tmp_path)()
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client,
            run_params(
                preset="antigravity/gemini-3.8-flash-medium",
                model_alias="gemini-3.8-flash-medium",
                deadline_seconds=15.0,
            ),
        )
        result = client.last_run_response.result
        assert result["status"] == "failed"
        assert events[-1].event.payload.code == "unsupported_model"
        assert session_spawns(tmp_path) == []
    finally:
        await client.aclose()


async def test_runner_rejects_model_alias_mismatch(runner_factory, tmp_path):
    runner = agy_runner(runner_factory, tmp_path)()
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client,
            run_params(
                preset=f"antigravity/{MODEL}",
                model_alias="some-other-model",
                deadline_seconds=15.0,
            ),
        )
        result = client.last_run_response.result
        assert result["status"] == "failed"
        assert events[-1].event.payload.code == "model_alias_mismatch"
        assert session_spawns(tmp_path) == []
    finally:
        await client.aclose()


async def test_runner_rejects_run_when_catalog_lacks_the_id(
    runner_factory, tmp_path
):
    runner = agy_runner(runner_factory, tmp_path, catalog="missing")()
    client = await RunnerClient.connect(runner.socket_path)
    try:
        models = await client.call("discover_models")
        by_id = {d["model_id"]: d for d in models.result["models"]}
        assert by_id[MODEL]["verification"]["status"] == "failed"

        events = await drive(
            client,
            run_params(
                preset=f"antigravity/{MODEL}",
                model_alias=MODEL,
                deadline_seconds=15.0,
            ),
        )
        result = client.last_run_response.result
        assert result["status"] == "failed"
        assert events[-1].event.payload.code == "catalog_not_verified"
        assert session_spawns(tmp_path) == []
    finally:
        await client.aclose()


async def test_runner_init_model_mismatch_fails_before_prompt(
    runner_factory, tmp_path
):
    runner = agy_runner(runner_factory, tmp_path, "wrong_model")()
    client = await RunnerClient.connect(runner.socket_path)
    try:
        await drive(
            client,
            run_params(
                preset=f"antigravity/{MODEL}",
                model_alias=MODEL,
                deadline_seconds=15.0,
            ),
        )
        result = client.last_run_response.result
        assert result["status"] == "failed"
        records = read_log(tmp_path)
        assert not any(r.get("event") == "prompt" for r in records)
    finally:
        await client.aclose()


async def test_runner_init_permission_mismatch_fails_before_prompt(
    runner_factory, tmp_path
):
    # The fixture reports always-proceed although the operator granted no
    # bypass: fail closed before the prompt is ever sent.
    runner = agy_runner(runner_factory, tmp_path, "wrong_permission")()
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client,
            run_params(
                preset=f"antigravity/{MODEL}",
                model_alias=MODEL,
                deadline_seconds=15.0,
            ),
        )
        result = client.last_run_response.result
        assert result["status"] == "failed"
        assert events[-1].event.payload.code == "permission_mode_mismatch"
        records = read_log(tmp_path)
        assert not any(r.get("event") == "prompt" for r in records)
    finally:
        await client.aclose()


async def test_runner_bypass_requires_action_grant_and_env_opt_in(
    runner_factory, tmp_path
):
    # Env opt-in alone is not enough: without the named action the flag stays off.
    runner = agy_runner(runner_factory, tmp_path, allow_skip=True)()
    client = await RunnerClient.connect(runner.socket_path)
    try:
        await drive(
            client,
            run_params(
                preset=f"antigravity/{MODEL}",
                model_alias=MODEL,
                deadline_seconds=15.0,
            ),
        )
        assert client.last_run_response.result["status"] == "completed"
        argv = session_spawns(tmp_path)[0]["argv"]
        assert "--dangerously-skip-permissions" not in argv
    finally:
        await client.aclose()


async def test_runner_bypass_flag_when_operator_grants_action(
    runner_factory, tmp_path
):
    runner = agy_runner(runner_factory, tmp_path, allow_skip=True)(
        actions=[SKIP_ACTION]
    )
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client,
            run_params(
                preset=f"antigravity/{MODEL}",
                model_alias=MODEL,
                deadline_seconds=15.0,
            ),
        )
        result = client.last_run_response.result
        assert result["status"] == "completed"
        argv = session_spawns(tmp_path)[0]["argv"]
        assert "--dangerously-skip-permissions" in argv
        # And the run still completed only because init reported always-proceed.
        assert events[-1].event.kind == "run.completed"
    finally:
        await client.aclose()


async def test_runner_bypass_grant_without_env_opt_in_stays_off(
    runner_factory, tmp_path
):
    runner = agy_runner(runner_factory, tmp_path)(actions=[SKIP_ACTION])
    client = await RunnerClient.connect(runner.socket_path)
    try:
        await drive(
            client,
            run_params(
                preset=f"antigravity/{MODEL}",
                model_alias=MODEL,
                deadline_seconds=15.0,
            ),
        )
        assert client.last_run_response.result["status"] == "completed"
        argv = session_spawns(tmp_path)[0]["argv"]
        assert "--dangerously-skip-permissions" not in argv
    finally:
        await client.aclose()


async def test_runner_denied_tool_is_partial_not_success(runner_factory, tmp_path):
    runner = agy_runner(runner_factory, tmp_path, "denied")()
    client = await RunnerClient.connect(runner.socket_path)
    try:
        await drive(
            client,
            run_params(
                preset=f"antigravity/{MODEL}",
                model_alias=MODEL,
                deadline_seconds=15.0,
            ),
        )
        result = client.last_run_response.result
        assert result["status"] == "completed"
        assert result["outcome"] == "partial"
    finally:
        await client.aclose()


async def test_runner_cli_error_status_is_failed(runner_factory, tmp_path):
    runner = agy_runner(runner_factory, tmp_path, "error_result")()
    client = await RunnerClient.connect(runner.socket_path)
    try:
        await drive(
            client,
            run_params(
                preset=f"antigravity/{MODEL}",
                model_alias=MODEL,
                deadline_seconds=15.0,
            ),
        )
        result = client.last_run_response.result
        assert result["status"] == "failed"
    finally:
        await client.aclose()


async def test_unbound_workspace_is_denied_before_any_spawn(
    runner_factory, tmp_path
):
    runner = agy_runner(runner_factory, tmp_path)()
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client,
            run_params(
                preset=f"antigravity/{MODEL}",
                model_alias=MODEL,
                workspace={"workspace_id": "ws-9"},
                deadline_seconds=15.0,
            ),
        )
        result = client.last_run_response.result
        assert result["status"] == "failed"
        assert events[-1].event.payload.code == "workspace_not_bound"
        assert read_log(tmp_path) == []
    finally:
        await client.aclose()


async def test_antigravity_without_execution_config_is_denied(
    runner_factory, tmp_path
):
    runner = agy_runner(runner_factory, tmp_path)(config=False)
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = await drive(
            client,
            run_params(
                preset=f"antigravity/{MODEL}",
                model_alias=MODEL,
                deadline_seconds=15.0,
            ),
        )
        result = client.last_run_response.result
        assert result["status"] == "failed"
        assert events[-1].event.payload.code == "execution_config_required"
        assert read_log(tmp_path) == []
    finally:
        await client.aclose()


async def test_runner_deadline_cancels_hung_agy_run(runner_factory, tmp_path):
    runner = agy_runner(runner_factory, tmp_path, "hang")(
        cancel_deadline=2.0,
    )
    client = await RunnerClient.connect(runner.socket_path)
    try:
        await drive(
            client,
            run_params(
                preset=f"antigravity/{MODEL}",
                model_alias=MODEL,
                deadline_seconds=0.5,
            ),
        )
        result = client.last_run_response.result
        assert result["status"] == "cancelled"
        assert result["outcome"] == "cancelled"
        await asyncio.sleep(0.2)
        assert session_spawns(tmp_path)
    finally:
        await client.aclose()
