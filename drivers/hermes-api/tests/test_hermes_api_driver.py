"""Hermes-API driver unit tests. Synthetic fixtures only — no real CLI/account."""

from __future__ import annotations

import asyncio
import gc
import importlib.metadata
import json
import os
import sys
import time
from pathlib import Path

import pytest

from cli_provider_sdk import (
    EventKind,
    Message,
    NormalizedRequest,
    ProviderDriver,
    RoleMode,
    RuntimeContext,
    SessionMode,
    StreamingMode,
    StructuredOutputMode,
    UsageProvenance,
    WorkspaceRef,
)
from cli_provider_transports import LocalProcessExecutor

from cli_driver_hermes_api import (
    HermesApiDriver,
    PRESETS,
    REASONING_LEVELS,
    YOLO_ACTION,
)

FAKE = Path(__file__).parent / "fake_hermes.py"
ENTRY_POINT_GROUP = "cli_provider.drivers"

BAI_PRESET = "bai/deepseek-v4.1-flash"
CC_PRESET = "commandcode/deepseek-v4.1-flash"
SECRET_VALUE = "sk-fixture-secret-000"


@pytest.fixture(autouse=True)
async def _collect_subprocess_transports():
    yield
    gc.collect()


def make_wrapper(tmp_path: Path) -> Path:
    wrapper = tmp_path / "hermes"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8"
    )
    wrapper.chmod(0o755)
    return wrapper


class AllowAll:
    def allows(self, action: str) -> bool:
        return True


class DenyAll:
    def allows(self, action: str) -> bool:
        return False


class TmpWorkspace:
    def __init__(self, root: Path) -> None:
        self._root = str(root)

    @property
    def root(self) -> str:
        return self._root

    def resolve(self, relative: str) -> str:
        return os.path.join(self._root, relative)


def make_ctx(
    tmp_path: Path,
    mode: str,
    *,
    capture: Path | None = None,
    workspace: bool = True,
    permissions=True,
    extra_env: dict[str, str] | None = None,
) -> RuntimeContext:
    env = dict(os.environ)
    env["FAKE_HERMES_MODE"] = mode
    env["BAI_API_KEY"] = SECRET_VALUE
    env["COMMANDCODE_API_KEY"] = SECRET_VALUE
    if capture is not None:
        env["FAKE_HERMES_CAPTURE"] = str(capture)
    env.update(extra_env or {})
    return RuntimeContext(
        executor=LocalProcessExecutor(env=env),
        workspace=TmpWorkspace(tmp_path / "ws") if workspace else None,
        permissions=AllowAll() if permissions is True else (
            DenyAll() if permissions is False else permissions),
    )


def make_request(
    *,
    run_id: str = "run_ha_1",
    deadline: float | None = 15.0,
    preset: str = BAI_PRESET,
    model_alias: str | None = None,
) -> NormalizedRequest:
    return NormalizedRequest(
        run_id=run_id,
        task_id="task_ha_1",
        attempt_id="attempt_ha_1",
        preset=preset,
        workspace=WorkspaceRef(workspace_id="ws-alpha"),
        model_alias=model_alias,
        messages=[Message(role="user", content="summarize the module")],
        deadline_seconds=deadline,
    )


async def collect(driver, request, ctx) -> list:
    return [event async for event in driver.execute(request, ctx)]


def kinds(events: list) -> list[str]:
    return [event.kind for event in events]


def answer_text(events: list) -> str:
    return "".join(
        event.payload.text for event in events if event.kind == EventKind.MESSAGE_DELTA
    )


def terminal(events: list):
    terms = [e for e in events if e.kind in (
        EventKind.RUN_COMPLETED, EventKind.RUN_FAILED, EventKind.RUN_CANCELLED)]
    assert len(terms) == 1, f"expected one terminal event, got {kinds(events)}"
    return terms[0]


def read_capture(capture: Path) -> dict:
    return json.loads(capture.read_text())


# ----------------------------------------------------------------- packaging


def test_entry_point_is_registered_for_the_runner_allowlist():
    entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    matches = [e for e in entry_points if e.name == "hermes-api"]
    assert matches, "hermes-api driver is not registered in cli_provider.drivers"
    entry = matches[0]
    assert entry.value == "cli_driver_hermes_api:HermesApiDriver"
    assert entry.dist is not None
    assert entry.dist.metadata["Name"].replace("_", "-").lower() == "cli-driver-hermes-api"
    assert entry.dist.version == "0.1.0"


def test_driver_satisfies_the_sdk_protocol_and_capabilities():
    driver = HermesApiDriver()
    assert isinstance(driver, ProviderDriver)
    manifest = driver.manifest
    assert manifest.driver_id == "hermes-api"
    assert manifest.synthetic is False
    caps = driver.capabilities()
    assert caps.internal_tools is True
    assert caps.external_tool_calls is False
    assert caps.streaming is StreamingMode.NATIVE
    assert caps.sessions is SessionMode.NONE
    assert caps.roles is RoleMode.SERIALIZED
    assert caps.structured_output is StructuredOutputMode.NONE
    assert caps.web_search is False


def test_presets_cover_both_backends_with_exact_pins():
    assert set(PRESETS) == {BAI_PRESET, CC_PRESET}
    assert PRESETS[BAI_PRESET].model_id == "deepseek-v4.1-flash"
    assert PRESETS[BAI_PRESET].base_url == "https://api.b.ai/v1"
    assert PRESETS[CC_PRESET].model_id == "deepseek/deepseek-v4.1-flash"
    assert PRESETS[CC_PRESET].base_url == "https://api.commandcode.ai/provider/v1"
    assert REASONING_LEVELS == frozenset({"low", "high", "max"})


# --------------------------------------------------------------------- probe


async def test_probe_reports_the_exact_cli_version(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    report = await driver.probe(make_ctx(tmp_path, "ok"))
    assert report.ok is True
    assert report.cli_version == "0.21.3"


async def test_probe_fails_closed_without_an_executor():
    report = await HermesApiDriver().probe(RuntimeContext())
    assert report.ok is False
    assert any("no process executor" in n for n in report.notes)


async def test_probe_refuses_a_version_that_does_not_match_the_pin(tmp_path):
    driver = HermesApiDriver(
        cli_command=str(make_wrapper(tmp_path)), expected_version="9.9.9"
    )
    report = await driver.probe(make_ctx(tmp_path, "ok"))
    assert report.ok is False
    assert report.cli_version == "0.21.3"


# ----------------------------------------------------------------- discovery


async def test_discovery_lists_operator_presets_without_claiming_verification(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    models = await driver.discover_models(make_ctx(tmp_path, "ok"))
    ids = sorted(m.model_id for m in models)
    assert ids == ["bai:deepseek-v4.1-flash", "commandcode:deepseek-v4.1-flash"]
    for m in models:
        assert m.verification.status.value == "unknown"


# ------------------------------------------------------------------- execute


async def test_happy_path_maps_only_text_records_to_answer(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "ok"))
    assert answer_text(events) == "fixture-answer-text"
    assert kinds(events).count(EventKind.TOOL_STARTED) == 1
    assert kinds(events).count(EventKind.TOOL_COMPLETED) == 1
    result = terminal(events)
    assert result.kind == EventKind.RUN_COMPLETED
    assert result.payload.outcome == "succeeded"


async def test_result_text_is_not_reemitted_as_a_delta(tmp_path):
    # hermes result.text duplicates the streamed answer; emitting it again
    # would double the answer the caller sees.
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "ok"))
    assert answer_text(events) == "fixture-answer-text"  # not doubled


async def test_reported_usage_is_used_and_zero_usage_stays_unknown(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "ok"))
    result = terminal(events)
    assert result.payload.usage.provenance == UsageProvenance.REPORTED
    assert result.payload.usage.input_tokens == 3
    assert result.payload.usage.output_tokens == 2

    events0 = await collect(driver, make_request(run_id="run_ha_z"),
                            make_ctx(tmp_path, "zero_usage"))
    result0 = terminal(events0)
    assert result0.payload.usage.provenance == UsageProvenance.UNKNOWN
    assert result0.payload.usage.input_tokens is None


async def test_tool_error_marks_run_partial_not_succeeded(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "tool_error"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_COMPLETED
    assert result.payload.outcome == "partial"


async def test_reasoning_reporting_is_configured_not_observed(tmp_path):
    driver = HermesApiDriver(
        cli_command=str(make_wrapper(tmp_path)), reasoning="high"
    )
    events = await collect(driver, make_request(), make_ctx(tmp_path, "ok"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_COMPLETED
    message = result.payload.message or ""
    assert "high" in message
    assert "unobserved" in message or "configured" in message


# ----------------------------------------------------- pre-spawn rejections


async def test_unknown_preset_fails_before_spawn(tmp_path):
    capture = tmp_path / "capture.json"
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(
        driver, make_request(preset="evil/other-model"),
        make_ctx(tmp_path, "ok", capture=capture))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "unknown_preset"
    assert not capture.exists(), "CLI must not be spawned for an unknown preset"


async def test_unsupported_reasoning_fails_before_spawn(tmp_path):
    capture = tmp_path / "capture.json"
    driver = HermesApiDriver(
        cli_command=str(make_wrapper(tmp_path)), reasoning="xhigh"
    )
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "ok", capture=capture))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "unsupported_reasoning"
    assert not capture.exists()


@pytest.mark.parametrize("bad", ["medium", "auto", "economy", "balanced", "thorough",
                                 "maximum", "minimal", "ultra", "none", "xhigh"])
async def test_internal_effort_hints_never_reach_the_cli(tmp_path, bad):
    capture = tmp_path / "capture.json"
    driver = HermesApiDriver(
        cli_command=str(make_wrapper(tmp_path)), reasoning=bad
    )
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "ok", capture=capture))
    assert terminal(events).payload.code == "unsupported_reasoning"
    assert not capture.exists()


async def test_no_executor_and_no_deadline_fail_closed(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    no_exec = await collect(driver, make_request(), RuntimeContext())
    assert terminal(no_exec).payload.code == "no_process_executor"
    no_deadline = await collect(
        driver, make_request(deadline=None), make_ctx(tmp_path, "ok"))
    assert terminal(no_deadline).payload.code == "no_deadline"


async def test_yolo_requires_preapproved_permissions(tmp_path):
    capture = tmp_path / "capture.json"
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(
        driver, make_request(),
        make_ctx(tmp_path, "ok", capture=capture, permissions=False))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "yolo_not_preapproved"
    assert not capture.exists()


async def test_missing_permissions_service_fails_closed(tmp_path):
    capture = tmp_path / "capture.json"
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    ctx = make_ctx(tmp_path, "ok", capture=capture)
    ctx.permissions = None
    events = await collect(driver, make_request(), ctx)
    assert terminal(events).payload.code == "yolo_not_preapproved"
    assert not capture.exists()


# ------------------------------------------------- wire/config/argv contract


async def test_argv_pins_provider_model_reasoning_and_bounds(tmp_path):
    capture = tmp_path / "capture.json"
    driver = HermesApiDriver(
        cli_command=str(make_wrapper(tmp_path)), reasoning="max", max_turns=7,
        run_budget_cap=111,
    )
    events = await collect(
        driver, make_request(deadline=500), make_ctx(tmp_path, "ok", capture=capture))
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    data = read_capture(capture)
    argv = data["argv"]
    assert argv[0] == "chat"
    assert "--query-file" in argv
    assert argv[argv.index("--provider") + 1] == "bai"
    assert argv[argv.index("--model") + 1] == "deepseek-v4.1-flash"
    assert argv[argv.index("--reasoning") + 1] == "max"
    assert argv[argv.index("--toolsets") + 1] == "file,terminal"
    assert argv[argv.index("--format") + 1] == "stream-json"
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--run-budget") + 1] == "111"
    assert "--oneshot" in argv and "--yolo" in argv and "--ignore-rules" in argv
    # custom-provider config must stay visible to the CLI: safe-mode ignores it.
    assert "--safe-mode" not in argv
    assert "--ignore-user-config" not in argv
    # Secrets never travel in argv.
    assert SECRET_VALUE not in json.dumps(argv)
    assert data["env_seen"]["HERMES_HOME"] is True
    assert data["env_seen"]["BAI_API_KEY"] is True
    assert Path(data["hermes_home"]).is_dir()


async def test_generated_config_is_strict_and_secret_free(tmp_path):
    capture = tmp_path / "capture.json"
    driver = HermesApiDriver(
        cli_command=str(make_wrapper(tmp_path)), reasoning="high"
    )
    await collect(driver, make_request(), make_ctx(tmp_path, "ok", capture=capture))
    data = read_capture(capture)
    cfg = json.loads(data["config"])  # emitted as JSON (valid YAML subset)
    assert SECRET_VALUE not in data["config"]
    assert cfg.get("fallback_providers") in (None, [], {})
    # Only the selected provider may be configured.
    providers = cfg.get("providers") or {}
    assert set(providers) == {"bai"}
    entry = providers["bai"]
    assert entry["base_url"] == "https://api.b.ai/v1"
    assert entry["api_mode"] == "chat_completions"
    assert entry["key_env"] == "BAI_API_KEY"
    assert "api_key" not in entry
    assert cfg["model"]["default"] == "deepseek-v4.1-flash"
    assert cfg["model"]["provider"] == "bai"
    assert cfg["auxiliary"]["title_generation"]["enabled"] is False
    assert cfg["memory"]["memory_enabled"] is False
    assert cfg["plugins"]["enabled"] == []
    assert cfg.get("mcp_servers") in (None, {})
    # Prompt rides a file, never argv.
    assert "summarize the module" in data["query"]


async def test_commandcode_preset_uses_builtin_profile_not_custom(tmp_path):
    capture = tmp_path / "capture.json"
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(
        driver, make_request(preset=CC_PRESET),
        make_ctx(tmp_path, "ok", capture=capture))
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    data = read_capture(capture)
    argv = data["argv"]
    assert argv[argv.index("--provider") + 1] == "commandcode"
    assert argv[argv.index("--model") + 1] == "deepseek/deepseek-v4.1-flash"
    cfg = json.loads(data["config"])
    # The bundled profile supplies base_url/endpoint behaviour; the config only
    # pins provider + base_url — a same-named custom entry would shadow it.
    providers = cfg.get("providers") or {}
    assert "commandcode" not in providers
    assert cfg["model"]["provider"] == "commandcode"
    assert cfg["model"]["base_url"] == "https://api.commandcode.ai/provider/v1"
    assert cfg["model"]["default"] == "deepseek/deepseek-v4.1-flash"


async def test_workspace_root_drives_dash_in_and_cwd(tmp_path):
    capture = tmp_path / "capture.json"
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    ctx = make_ctx(tmp_path, "ok", capture=capture)
    ws = tmp_path / "ws"
    ws.mkdir()
    await collect(driver, make_request(), ctx)
    data = read_capture(capture)
    argv = data["argv"]
    assert argv[argv.index("--in") + 1] == str(ws)
    assert data["cwd"] == str(ws)


async def test_missing_workspace_falls_back_to_task_local_dir(tmp_path):
    capture = tmp_path / "capture.json"
    driver = HermesApiDriver(
        cli_command=str(make_wrapper(tmp_path)), state_dir=str(tmp_path / "state")
    )
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "ok", capture=capture, workspace=False))
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    data = read_capture(capture)
    assert data["hermes_home"].startswith(str(tmp_path / "state"))
    argv = data["argv"]
    workdir = argv[argv.index("--in") + 1]
    assert workdir.startswith(data["hermes_home"])


async def test_model_alias_mismatch_is_rejected_before_spawn(tmp_path):
    capture = tmp_path / "capture.json"
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(
        driver, make_request(model_alias="bai/other-model"),
        make_ctx(tmp_path, "ok", capture=capture))
    assert terminal(events).payload.code == "model_alias_mismatch"
    assert not capture.exists()


async def test_init_model_mismatch_fails_the_run(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "model_mismatch"))
    assert terminal(events).payload.code == "model_mismatch"


# ------------------------------------------------------- protocol boundaries


async def test_malformed_frame_is_a_protocol_error_not_success(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "malformed"))
    assert terminal(events).payload.code == "protocol_error"


async def test_oversize_frame_is_rejected(tmp_path):
    driver = HermesApiDriver(
        cli_command=str(make_wrapper(tmp_path)), max_frame_bytes=4096
    )
    events = await collect(driver, make_request(), make_ctx(tmp_path, "oversize"))
    assert terminal(events).payload.code == "protocol_error"


async def test_eof_without_result_is_never_success(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "no_result"))
    assert terminal(events).payload.code == "missing_result"


async def test_frame_before_init_is_refused(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "no_init"))
    assert terminal(events).payload.code == "protocol_error"


async def test_undocumented_record_type_is_refused(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "unknown_event"))
    assert terminal(events).payload.code == "unknown_event"


async def test_cli_reported_error_is_failed_not_completed(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "error_result"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "cli_reported_error"


async def test_nonzero_exit_code_in_result_frame_is_failed(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "nonzero_exit"))
    assert terminal(events).kind == EventKind.RUN_FAILED


async def test_stderr_never_reaches_events_or_answer(tmp_path):
    driver = HermesApiDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), make_ctx(tmp_path, "stderr_noise"))
    assert answer_text(events) == "fixture-answer-text"
    serialized = json.dumps([e.model_dump(mode="json") for e in events])
    assert "SECRET-STDERR-TOKEN" not in serialized
    # And a provider-side error message gets redacted/bounded too.
    events2 = await collect(driver, make_request(run_id="run_ha_e"),
                            make_ctx(tmp_path, "error_result"))
    serialized2 = json.dumps([e.model_dump(mode="json") for e in events2])
    assert "sk-fixturesecret-deadbeef" not in serialized2


# --------------------------------------------------------------- cancellation


async def test_deadline_cancels_the_cli_without_claiming_success(tmp_path):
    driver = HermesApiDriver(
        cli_command=str(make_wrapper(tmp_path)), grace_seconds=0.5
    )
    events = await collect(
        driver, make_request(deadline=0.6), make_ctx(tmp_path, "hang"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_CANCELLED
    assert "deadline" in result.payload.reason


async def test_cancel_is_confirmed_and_reported_as_cancelled(tmp_path):
    driver = HermesApiDriver(
        cli_command=str(make_wrapper(tmp_path)), grace_seconds=0.5
    )
    request = make_request(deadline=30.0)
    ctx = make_ctx(tmp_path, "hang")
    events: list = []

    async def consume() -> None:
        async for event in driver.execute(request, ctx):
            events.append(event)

    task = asyncio.ensure_future(consume())
    for _ in range(100):
        if events:
            break
        await asyncio.sleep(0.05)
    assert events, "the run never started"
    result = await driver.cancel(request.run_id, ctx)
    assert result.requested is True
    assert result.confirmed is True
    await asyncio.wait_for(task, timeout=15)
    assert terminal(events).kind == EventKind.RUN_CANCELLED
