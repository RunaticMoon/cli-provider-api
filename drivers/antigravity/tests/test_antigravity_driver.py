"""Unit tests for the Antigravity stream-json driver.

The fixture is a Python double of the official ``agy`` CLI speaking the
documented ``--input-format stream-json --output-format stream-json``
protocol (``init`` / ``step_update`` / ``result`` envelopes, each payload
nested under a key matching the event name). Tests exercise the real
process tree — real subprocesses, real pipes, real process-group teardown —
but no real CLI or credential.
"""

import asyncio
import gc
import json
import os
import sys
import time
from pathlib import Path

import pytest
from cli_driver_antigravity import AntigravityDriver
from cli_provider_sdk import (
    EventKind,
    Message,
    NormalizedRequest,
    RuntimeContext,
    UsageProvenance,
    VerificationStatus,
    WorkspaceRef,
)
from cli_provider_transports.process import LocalProcessExecutor

pytestmark = pytest.mark.anyio

FIXTURE = Path(__file__).parent / "fixtures" / "fake_agy.py"

MODEL = "gemini-3.8-flash-high"
MODEL_2 = "claude-opus-4-6-thinking"
PINNED_VERSION = "1.2.7"
SKIP_ACTION = "antigravity.dangerously_skip_permissions"


def make_wrapper(tmp_path: Path) -> Path:
    wrapper = tmp_path / "agy"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}" "$@"\n', encoding="utf-8"
    )
    wrapper.chmod(0o755)
    return wrapper


def driver_for(tmp_path: Path, **kwargs) -> AntigravityDriver:
    """Driver configured the way an operator would pin it."""
    kwargs.setdefault("models", [MODEL, MODEL_2])
    kwargs.setdefault("expected_version", PINNED_VERSION)
    return AntigravityDriver(cli_command=str(make_wrapper(tmp_path)), **kwargs)


class StaticPermissions:
    """Operator-supplied permission policy double: allows only listed actions."""

    def __init__(self, allowed=None) -> None:
        self._allowed = set(allowed or set())
        self.queries: list[str] = []

    def allows(self, action: str) -> bool:
        self.queries.append(action)
        return action in self._allowed


class FixedWorkspace:
    def __init__(self, root: str) -> None:
        self._root = root

    @property
    def root(self) -> str:
        return self._root

    def resolve(self, relative: str) -> str:
        return os.path.join(self._root, relative)


def make_ctx(
    tmp_path: Path,
    mode: str = "ok",
    *,
    catalog: str = "ok",
    logfile: Path | None = None,
    extra_env: dict[str, str] | None = None,
    permissions=None,
    workspace="default",
) -> RuntimeContext:
    env = dict(os.environ)
    env["FAKE_AGY_MODE"] = mode
    env["FAKE_AGY_CATALOG"] = catalog
    env["FAKE_AGY_VERSION"] = PINNED_VERSION
    if logfile is not None:
        env["FAKE_AGY_LOG"] = str(logfile)
    env.update(extra_env or {})
    return RuntimeContext(
        executor=LocalProcessExecutor(env=env),
        permissions=permissions,
        workspace=FixedWorkspace(str(tmp_path)) if workspace == "default" else workspace,
    )


def read_log(logfile: Path) -> list[dict]:
    if not logfile.exists():
        return []
    return [json.loads(line) for line in logfile.read_text().splitlines() if line.strip()]


def session_spawns(logfile: Path) -> list[dict]:
    """Records where the fixture was spawned as a session (not --version/models)."""
    return [
        record
        for record in read_log(logfile)
        if record["event"] == "start"
        and "--version" not in record["argv"]
        and "models" not in record["argv"]
    ]


def prompts(logfile: Path) -> list[dict]:
    return [record for record in read_log(logfile) if record["event"] == "prompt"]


def make_request(
    *,
    run_id: str = "run_1",
    deadline: float | None = 15.0,
    preset: str = f"antigravity/{MODEL}",
    model_alias: str | None = MODEL,
) -> NormalizedRequest:
    return NormalizedRequest(
        run_id=run_id,
        task_id="task_1",
        attempt_id="attempt_1",
        preset=preset,
        workspace=WorkspaceRef(workspace_id="ws-alpha"),
        model_alias=model_alias,
        messages=[Message(role="user", content="summarize the module")],
        deadline_seconds=deadline,
    )


async def collect(driver: AntigravityDriver, request: NormalizedRequest, ctx: RuntimeContext) -> list:
    return [event async for event in driver.execute(request, ctx)]


def kinds(events: list) -> list[str]:
    return [event.kind for event in events]


def answer_text(events: list) -> str:
    return "".join(
        event.payload.text for event in events if event.kind == EventKind.MESSAGE_DELTA
    )


def serialized(events: list) -> str:
    return json.dumps(
        [event.model_dump(mode="json") for event in events], ensure_ascii=False
    )


def terminal(events: list):
    terminals = [
        event
        for event in events
        if event.kind
        in (EventKind.RUN_COMPLETED, EventKind.RUN_FAILED, EventKind.RUN_CANCELLED)
    ]
    assert len(terminals) == 1, f"expected exactly one terminal event, got {kinds(events)}"
    return terminals[0]


# ---------------------------------------------------------------- discovery


async def test_probe_reports_cli_version(tmp_path):
    driver = driver_for(tmp_path)
    result = await driver.probe(make_ctx(tmp_path))
    assert result.ok is True
    assert result.cli_version == PINNED_VERSION


async def test_probe_without_executor_fails_cleanly():
    driver = AntigravityDriver(cli_command="/nonexistent/agy")
    result = await driver.probe(RuntimeContext())
    assert result.ok is False


async def test_probe_malformed_version_fails(tmp_path):
    driver = driver_for(tmp_path)
    ctx = make_ctx(tmp_path, "bad_version")
    result = await driver.probe(ctx)
    assert result.ok is False


async def test_probe_version_pin_mismatch_fails(tmp_path):
    driver = driver_for(tmp_path, expected_version="0.0.0")
    result = await driver.probe(make_ctx(tmp_path))
    assert result.ok is False


async def test_discover_models_marks_pinned_catalog_ids_passed(tmp_path):
    driver = driver_for(tmp_path)
    descriptors = await driver.discover_models(make_ctx(tmp_path))
    by_id = {d.model_id: d for d in descriptors}
    assert by_id[MODEL].verification.status == VerificationStatus.PASSED
    assert by_id[MODEL_2].verification.status == VerificationStatus.PASSED
    assert PINNED_VERSION in (by_id[MODEL].verification.reason or "")


async def test_discover_models_absent_exact_id_stays_failed(tmp_path):
    driver = driver_for(tmp_path)
    descriptors = await driver.discover_models(make_ctx(tmp_path, catalog="missing"))
    by_id = {d.model_id: d for d in descriptors}
    assert by_id[MODEL].verification.status == VerificationStatus.FAILED
    assert by_id[MODEL_2].verification.status == VerificationStatus.PASSED


async def test_discover_models_prefix_suffix_collision_not_promoted(tmp_path):
    # Catalog rows that extend or prefix the trusted ID exist, but the exact
    # ID itself is absent: it must not be promoted.
    driver = driver_for(tmp_path)
    descriptors = await driver.discover_models(make_ctx(tmp_path, catalog="missing"))
    by_id = {d.model_id: d for d in descriptors}
    assert by_id[MODEL].verification.status == VerificationStatus.FAILED


async def test_discover_models_malformed_catalog_is_unknown(tmp_path):
    driver = driver_for(tmp_path)
    descriptors = await driver.discover_models(make_ctx(tmp_path, catalog="malformed"))
    assert {d.verification.status for d in descriptors} == {VerificationStatus.UNKNOWN}


async def test_discover_models_nonzero_catalog_exit_is_unknown(tmp_path):
    driver = driver_for(tmp_path)
    descriptors = await driver.discover_models(make_ctx(tmp_path, catalog="nonzero"))
    assert {d.verification.status for d in descriptors} == {VerificationStatus.UNKNOWN}


async def test_discover_models_catalog_timeout_is_unknown(tmp_path):
    driver = driver_for(tmp_path, catalog_timeout_seconds=0.5)
    descriptors = await driver.discover_models(make_ctx(tmp_path, catalog="hang"))
    assert {d.verification.status for d in descriptors} == {VerificationStatus.UNKNOWN}


async def test_discover_models_without_version_pin_is_unknown(tmp_path):
    wrapper = make_wrapper(tmp_path)
    driver = AntigravityDriver(cli_command=str(wrapper), models=[MODEL])
    descriptors = await driver.discover_models(make_ctx(tmp_path))
    assert [d.verification.status for d in descriptors] == [VerificationStatus.UNKNOWN]


async def test_discover_models_version_mismatch_is_failed(tmp_path):
    driver = driver_for(tmp_path, expected_version="0.0.0")
    descriptors = await driver.discover_models(make_ctx(tmp_path))
    assert {d.verification.status for d in descriptors} == {VerificationStatus.FAILED}


async def test_discover_models_without_executor_is_unknown(tmp_path):
    driver = driver_for(tmp_path)
    descriptors = await driver.discover_models(RuntimeContext())
    assert {d.verification.status for d in descriptors} == {VerificationStatus.UNKNOWN}


async def test_discover_models_no_pins_is_empty(tmp_path):
    driver = AntigravityDriver(cli_command=str(make_wrapper(tmp_path)), models=[])
    assert await driver.discover_models(make_ctx(tmp_path)) == []


async def test_discover_models_does_not_leak_catalog_stderr(tmp_path):
    driver = driver_for(tmp_path)
    descriptors = await driver.discover_models(make_ctx(tmp_path, catalog="nonzero"))
    blob = json.dumps([d.model_dump(mode="json") for d in descriptors])
    assert "authentication required" not in blob


def test_capabilities_are_conservative(tmp_path):
    driver = driver_for(tmp_path)
    capabilities = driver.capabilities()
    assert capabilities.workspace_write is False
    assert capabilities.usage == UsageProvenance.REPORTED


def test_capabilities_workspace_write_requires_bypass_opt_in(tmp_path):
    driver = driver_for(tmp_path, allow_skip_permissions=True)
    capabilities = driver.capabilities()
    assert capabilities.workspace_write is True


# ------------------------------------------------------------- model binding


async def test_run_binds_the_admitted_request_model(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver,
        make_request(preset=f"antigravity/{MODEL_2}", model_alias=MODEL_2),
        make_ctx(tmp_path, logfile=logfile),
    )
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    spawns = session_spawns(logfile)
    assert len(spawns) == 1
    argv = spawns[0]["argv"]
    assert argv[argv.index("--model") + 1] == MODEL_2


async def test_run_preset_derived_model_when_alias_absent(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver,
        make_request(model_alias=None),
        make_ctx(tmp_path, logfile=logfile),
    )
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    argv = session_spawns(logfile)[0]["argv"]
    assert argv[argv.index("--model") + 1] == MODEL


async def test_run_rejects_model_alias_mismatch(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver,
        make_request(model_alias="some-other-model"),
        make_ctx(tmp_path, logfile=logfile),
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "model_alias_mismatch"
    assert session_spawns(logfile) == []


async def test_run_rejects_model_outside_allowlist(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver,
        make_request(
            preset="antigravity/gemini-3.8-flash-medium",
            model_alias="gemini-3.8-flash-medium",
        ),
        make_ctx(tmp_path, logfile=logfile),
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "unsupported_model"
    assert session_spawns(logfile) == []


async def test_run_rejects_unknown_preset_shape(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(
        driver,
        make_request(preset="agy/review", model_alias=None),
        make_ctx(tmp_path),
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "unknown_preset"


async def test_run_rejects_model_not_in_catalog(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver,
        make_request(),
        make_ctx(tmp_path, catalog="missing", logfile=logfile),
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "catalog_not_verified"
    assert session_spawns(logfile) == []


async def test_run_requires_executor():
    driver = AntigravityDriver()
    events = await collect(driver, make_request(), RuntimeContext())
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "no_process_executor"


async def test_run_requires_deadline(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(deadline=None), make_ctx(tmp_path)
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "no_deadline"


async def test_run_requires_workspace(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, workspace=None)
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "no_workspace"


# --------------------------------------------------------------- permissions


async def test_run_default_omits_skip_permissions_flag(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, logfile=logfile)
    )
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    argv = session_spawns(logfile)[0]["argv"]
    assert "--dangerously-skip-permissions" not in argv


async def test_run_bypass_requires_env_opt_in_and_permission_grant(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path, allow_skip_permissions=True)
    permissions = StaticPermissions({SKIP_ACTION})
    events = await collect(
        driver,
        make_request(),
        make_ctx(tmp_path, logfile=logfile, permissions=permissions),
    )
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    argv = session_spawns(logfile)[0]["argv"]
    assert "--dangerously-skip-permissions" in argv
    assert SKIP_ACTION in permissions.queries


async def test_run_grant_without_env_opt_in_stays_conservative(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)  # allow_skip_permissions unset
    permissions = StaticPermissions({SKIP_ACTION})
    events = await collect(
        driver,
        make_request(),
        make_ctx(tmp_path, logfile=logfile, permissions=permissions),
    )
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    argv = session_spawns(logfile)[0]["argv"]
    assert "--dangerously-skip-permissions" not in argv


async def test_run_env_opt_in_without_grant_stays_conservative(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path, allow_skip_permissions=True)
    permissions = StaticPermissions()  # grants nothing
    events = await collect(
        driver,
        make_request(),
        make_ctx(tmp_path, logfile=logfile, permissions=permissions),
    )
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    argv = session_spawns(logfile)[0]["argv"]
    assert "--dangerously-skip-permissions" not in argv


async def test_run_scrubs_operator_env_from_child(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)
    dirty = {
        "AGY_MODEL": "decoy-model",
        "AGY_MODELS": "decoy-model",
        "AGY_EXPECTED_VERSION": "0.0.0",
        "AGY_ALLOW_SKIP_PERMISSIONS": "1",
    }
    events = await collect(
        driver,
        make_request(),
        make_ctx(tmp_path, logfile=logfile, extra_env=dirty),
    )
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    spawn = session_spawns(logfile)[0]
    assert spawn["agy_env"] == {}
    assert spawn["cwd"] == os.path.realpath(str(tmp_path)) or spawn["cwd"] == str(tmp_path)


# --------------------------------------------------------- init verification


async def test_run_rejects_init_model_mismatch_before_prompt(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "wrong_model", logfile=logfile)
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "model_mismatch"
    assert prompts(logfile) == []


async def test_run_rejects_init_missing_model(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "missing_model", logfile=logfile)
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "model_mismatch"
    assert prompts(logfile) == []


async def test_run_rejects_init_permission_mismatch_before_prompt(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver,
        make_request(),
        make_ctx(tmp_path, "wrong_permission", logfile=logfile),
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "permission_mode_mismatch"
    assert prompts(logfile) == []


async def test_run_rejects_init_permission_mismatch_under_bypass(tmp_path):
    # Operator enabled bypass but the CLI reports a different mode: fail.
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path, allow_skip_permissions=True)
    permissions = StaticPermissions({SKIP_ACTION})
    events = await collect(
        driver,
        make_request(),
        make_ctx(tmp_path, "wrong_permission", logfile=logfile, permissions=permissions),
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "permission_mode_mismatch"
    assert prompts(logfile) == []


async def test_run_rejects_init_missing_permission_mode(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "missing_perm", logfile=logfile)
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "permission_mode_mismatch"
    assert prompts(logfile) == []


async def test_run_rejects_init_wrong_cwd(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "wrong_cwd", logfile=logfile)
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "cwd_mismatch"
    assert prompts(logfile) == []


async def test_run_rejects_stream_without_init(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "no_init")
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "protocol_error"


async def test_run_init_handshake_is_bounded(tmp_path):
    driver = driver_for(tmp_path, handshake_timeout_seconds=0.5)
    started = time.monotonic()
    events = await collect(
        driver, make_request(deadline=15.0), make_ctx(tmp_path, "never_init")
    )
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, "missing init consumed the whole run deadline"
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "missing_init"


# ----------------------------------------------------------- stream contract


async def test_run_streams_only_agent_response_text(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), make_ctx(tmp_path))
    result = terminal(events)
    assert result.kind == EventKind.RUN_COMPLETED
    assert result.payload.outcome == "succeeded"
    assert answer_text(events) == "hello world"
    assert "hi\n" not in answer_text(events)
    usage = result.payload.usage
    assert usage.provenance == UsageProvenance.REPORTED
    assert usage.input_tokens == 11
    assert usage.output_tokens == 2


async def test_run_reports_tool_start_and_completion(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), make_ctx(tmp_path))
    started = [e for e in events if e.kind == EventKind.TOOL_STARTED]
    completed = [e for e in events if e.kind == EventKind.TOOL_COMPLETED]
    assert len(started) == 1 and started[0].payload.name == "run_command"
    assert len(completed) == 1 and completed[0].payload.status == "completed"


async def test_run_planning_steps_never_become_answer(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "planning_delta")
    )
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    assert answer_text(events) == "visible answer"
    assert "SECRET-PLAN" not in serialized(events)


async def test_run_tool_denial_is_partial_not_success(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), make_ctx(tmp_path, "denied"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_COMPLETED
    assert result.payload.outcome == "partial"
    failed_tools = [
        e
        for e in events
        if e.kind == EventKind.TOOL_COMPLETED and e.payload.status == "failed"
    ]
    assert failed_tools


async def test_run_error_result_is_failed_not_completed(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "error_result")
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "cli_reported_error"


async def test_run_unknown_terminal_status_is_failed(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "unknown_status")
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "cli_reported_error"


async def test_run_missing_terminal_status_is_failed(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "missing_status")
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "protocol_error"


async def test_run_canceled_result_without_cancel_is_failed(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "canceled_result")
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED


async def test_run_success_without_usage_is_unknown_not_fabricated(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "usage_absent")
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_COMPLETED
    assert result.payload.usage.provenance == UsageProvenance.UNKNOWN
    assert result.payload.usage.input_tokens is None


async def test_run_zero_usage_is_unknown_not_fabricated(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "usage_zero")
    )
    result = terminal(events)
    assert result.payload.usage.provenance == UsageProvenance.UNKNOWN


async def test_run_eof_without_result_is_failed(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), make_ctx(tmp_path, "no_result"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "missing_result"


async def test_malformed_frame_is_failed_not_completed(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), make_ctx(tmp_path, "malformed"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "protocol_error"


async def test_oversized_frame_is_failed(tmp_path):
    driver = driver_for(tmp_path, max_frame_bytes=1024)
    events = await collect(driver, make_request(), make_ctx(tmp_path, "oversize"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "protocol_error"


async def test_deeply_nested_frame_is_failed(tmp_path):
    driver = driver_for(tmp_path, max_frame_bytes=1024)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "deeply_nested")
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED


async def test_unknown_event_fails_closed(tmp_path):
    # An envelope outside the documented init/step_update/result set is a
    # stream we refuse to interpret rather than guess at.
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "unknown_event")
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "unknown_event"
    assert answer_text(events) == ""


async def test_cli_stderr_is_never_answer_text(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "stderr_noise")
    )
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    assert "sekrit" not in serialized(events)
    assert answer_text(events) == "hello"


async def test_ignores_sigterm_is_killed(tmp_path):
    driver = driver_for(tmp_path, grace_seconds=0.2)
    started = time.monotonic()
    events = await collect(
        driver, make_request(deadline=0.4), make_ctx(tmp_path, "ignore_term")
    )
    assert terminal(events).kind == EventKind.RUN_CANCELLED
    assert time.monotonic() - started < 10.0


async def test_descendant_outliving_leader_does_not_extend_the_run(tmp_path):
    driver = driver_for(tmp_path, grace_seconds=0.3)
    started = time.monotonic()
    events = await collect(
        driver,
        make_request(deadline=10.0),
        make_ctx(tmp_path, "leader_exits_descendant_ignores"),
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "missing_init"
    assert time.monotonic() - started < 10.0


async def test_cli_exit_code_is_not_treated_as_success(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), make_ctx(tmp_path, "no_result"))
    assert not any(e.kind == EventKind.RUN_COMPLETED for e in events)


# --------------------------------------------------------- cancel + deadline


async def test_cancel_terminates_process_group(tmp_path):
    driver = driver_for(tmp_path)
    request = make_request(deadline=60.0)
    ctx = make_ctx(tmp_path, "hang")
    events: list = []

    async def consume() -> None:
        async for event in driver.execute(request, ctx):
            events.append(event)

    task = asyncio.ensure_future(consume())
    for _ in range(100):
        if any(e.kind == EventKind.RUN_STARTED for e in events):
            break
        await asyncio.sleep(0.05)
    assert any(e.kind == EventKind.RUN_STARTED for e in events)

    result = await driver.cancel(request.run_id, ctx)
    await asyncio.wait_for(task, timeout=15)

    assert result.confirmed is True
    assert terminal(events).kind == EventKind.RUN_CANCELLED


async def test_deadline_cancels_run_and_kills_process(tmp_path):
    driver = driver_for(tmp_path)
    started = time.monotonic()
    events = await collect(
        driver, make_request(deadline=0.5), make_ctx(tmp_path, "hang")
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_CANCELLED
    assert "deadline" in result.payload.reason
    assert time.monotonic() - started < 10.0


async def test_cancel_unknown_run_returns_not_found(tmp_path):
    driver = driver_for(tmp_path)
    result = await driver.cancel("no-such-run", make_ctx(tmp_path))
    assert result.confirmed is False
    assert "no active" in result.detail


async def test_cancel_and_result_race_emits_exactly_one_terminal(tmp_path):
    driver = driver_for(tmp_path)
    request = make_request(deadline=60.0)
    ctx = make_ctx(tmp_path)
    events: list = []

    async def consume() -> None:
        async for event in driver.execute(request, ctx):
            events.append(event)
            if event.kind == EventKind.RUN_STARTED:
                asyncio.ensure_future(driver.cancel(request.run_id, ctx))

    await asyncio.wait_for(consume(), timeout=15)
    terminals = [
        e
        for e in events
        if e.kind
        in (EventKind.RUN_COMPLETED, EventKind.RUN_FAILED, EventKind.RUN_CANCELLED)
    ]
    assert len(terminals) == 1
    assert terminals[0].kind in (EventKind.RUN_COMPLETED, EventKind.RUN_CANCELLED)


# ------------------------------------------------------------- process stubs


class _BlockingWriter:
    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        await asyncio.sleep(600)

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _BrokenWriter:
    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc or BrokenPipeError("stdin closed")

    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        raise self._exc

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


def _eof_reader() -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_eof()
    return reader


def _fed_reader(lines: list[bytes]) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data(line)
    reader.feed_eof()
    return reader


def _init_line(cwd: str, model: str = MODEL, permission_mode: str = "request-review") -> bytes:
    return (
        json.dumps(
            {
                "event": "init",
                "conversation_id": "stub",
                "init": {
                    "cwd": cwd,
                    "model": model,
                    "permission_mode": permission_mode,
                    "tools": [],
                },
            }
        )
        + "\n"
    ).encode()


def _catalog_lines() -> list[bytes]:
    return [
        b"Fetching available models...\n",
        f"{MODEL}\tGemini 3.8 Flash (High)\n".encode(),
        f"{MODEL_2}\tClaude Opus 4.6 (Thinking)\n".encode(),
    ]


class _StubProcess:
    def __init__(self, stdin, stdout, stderr, returncode: int | None = None) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.pid = 0
        self.returncode = returncode

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


class _StubExecutor:
    """Answers --version/models probes from canned data; returns the given
    stub process for session spawns."""

    def __init__(self, session_process, *, version: str = PINNED_VERSION) -> None:
        self._session = session_process
        self._version = version
        self.calls: list[list[str]] = []

    async def spawn(self, argv, *, cwd=None):
        argv = list(argv)
        self.calls.append(argv)
        if "models" in argv:
            return _StubProcess(
                _BlockingWriter(),
                _fed_reader(_catalog_lines()),
                _eof_reader(),
                returncode=0,
            )
        if "--version" in argv:
            return _StubProcess(
                _BlockingWriter(),
                _fed_reader([f"{self._version}\n".encode()]),
                _eof_reader(),
                returncode=0,
            )
        return self._session


def _stub_ctx(process, workspace_root: str, permissions=None) -> RuntimeContext:
    return RuntimeContext(
        executor=_StubExecutor(process),
        permissions=permissions,
        workspace=FixedWorkspace(workspace_root),
    )


async def test_stalled_prompt_write_is_bounded_and_never_a_success():
    root = os.path.realpath(os.getcwd())
    process = _StubProcess(
        _BlockingWriter(), _fed_reader([_init_line(root)]), _eof_reader()
    )
    driver = AntigravityDriver(
        cli_command="agy", models=[MODEL], expected_version=PINNED_VERSION
    )
    started = time.monotonic()
    events = await collect(
        driver, make_request(deadline=0.4), _stub_ctx(process, root)
    )
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, "a stalled prompt write was not bounded by the deadline"
    assert not any(event.kind == EventKind.RUN_COMPLETED for event in events)


class _CancelAwareBrokenWriter:
    """Fails the prompt write only once the run has actually been cancelled."""

    def __init__(self, lookup) -> None:
        self._lookup = lookup

    def write(self, data: bytes) -> None:
        return None

    async def drain(self) -> None:
        while True:
            event = self._lookup()
            if event is not None and event.is_set():
                raise BrokenPipeError("CLI killed while the prompt was in flight")
            await asyncio.sleep(0.01)


async def test_cancel_during_the_prompt_write_is_not_a_provider_failure():
    root = os.path.realpath(os.getcwd())
    driver = AntigravityDriver(
        cli_command="agy", models=[MODEL], expected_version=PINNED_VERSION
    )
    request = make_request(deadline=10.0)
    writer = _CancelAwareBrokenWriter(
        lambda: driver._cancel_events.get(request.run_id)
    )
    process = _StubProcess(writer, _fed_reader([_init_line(root)]), _eof_reader())
    ctx = _stub_ctx(process, root)
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

    # The stub has pid 0, so termination can never be confirmed: the documented
    # contract is then to end without a terminal event (the Runner records
    # unknown) rather than to report a provider failure.
    result = await driver.cancel(request.run_id, ctx)
    assert result.confirmed is False
    await asyncio.wait_for(task, timeout=15)
    assert not any(
        event.kind
        in (EventKind.RUN_COMPLETED, EventKind.RUN_FAILED, EventKind.RUN_CANCELLED)
        for event in events
    ), kinds(events)


async def test_broken_stdin_is_a_failed_run_not_an_exception():
    root = os.path.realpath(os.getcwd())
    process = _StubProcess(
        _BrokenWriter(BrokenPipeError("stdin closed")),
        _fed_reader([_init_line(root)]),
        _eof_reader(),
    )
    driver = AntigravityDriver(
        cli_command="agy", models=[MODEL], expected_version=PINNED_VERSION
    )
    events = await collect(
        driver, make_request(), _stub_ctx(process, root)
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "cli_broken_pipe"


# ------------------------------------------------------------ resource use


async def test_repeated_runs_do_not_leak_descriptors(tmp_path):
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("descriptor accounting requires /proc")
    driver = driver_for(tmp_path, grace_seconds=0.5)
    ctx = make_ctx(tmp_path)
    # Leaked asyncio subprocess transports are reference cycles: a generational
    # collection during the loop could release them and mask the leak, so the
    # collector is disabled for the measurement window.
    gc.collect()
    gc.disable()
    try:
        before = len(os.listdir("/proc/self/fd"))
        for index in range(10):
            request = make_request(run_id=f"run_leak_{index}")
            events = await collect(driver, request, ctx)
            assert terminal(events).kind == EventKind.RUN_COMPLETED
        after = len(os.listdir("/proc/self/fd"))
    finally:
        gc.enable()
    # One incidental descriptor is tolerated; a leaked transport would show more.
    assert after - before <= 1, f"descriptor growth across runs: {before} -> {after}"


# ------------------------------------------- dynamic catalog / effort modes


async def test_discovery_lists_full_catalog_with_executable_flags(tmp_path):
    """Discovery enumerates every catalog row; the allowlist stays a separate
    execution gate reflected truthfully per descriptor."""
    driver = driver_for(tmp_path)  # allowlist = [MODEL, MODEL_2]
    descriptors = await driver.discover_models(make_ctx(tmp_path))
    by_id = {d.model_id: d for d in descriptors}
    assert set(by_id) == {
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "claude-opus-4-6-thinking",
        "claude-sonnet-4-6",
        "gemini-3.8-flash-high-x",
        "xgemini-3.8-flash-high",
    }
    assert all(
        d.verification.status == VerificationStatus.PASSED for d in descriptors
    )
    assert by_id[MODEL].executable is True
    assert by_id[MODEL_2].executable is True
    assert by_id["gemini-3.8-flash-medium"].executable is False
    # The native catalog carries no per-model effort evidence.
    assert all(d.effort.value == "unknown" for d in descriptors)
    assert by_id["claude-sonnet-4-6"].display_name == "Claude Sonnet 4.6"


async def test_star_allowlist_runs_catalog_member_without_preset_pin(tmp_path):
    """AGY_MODELS=* is the explicit operator opt-in: a verified catalog member
    executes with no per-model pin."""
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path, models=["*"])
    events = await collect(
        driver,
        make_request(
            preset="antigravity/gemini-3.8-flash-medium",
            model_alias="gemini-3.8-flash-medium",
        ),
        make_ctx(tmp_path, logfile=logfile),
    )
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    argv = session_spawns(logfile)[0]["argv"]
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-medium"


async def test_catalog_member_outside_allowlist_has_zero_effects(tmp_path):
    """Discovery alone never authorizes: a verified catalog member outside the
    allowlist fails before any session spawn."""
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)  # allowlist = [MODEL, MODEL_2]
    events = await collect(
        driver,
        make_request(
            preset="antigravity/claude-sonnet-4-6",
            model_alias="claude-sonnet-4-6",
        ),
        make_ctx(tmp_path, logfile=logfile),
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "unsupported_model"
    assert session_spawns(logfile) == []


async def test_effort_rejected_when_catalog_declares_no_support(tmp_path):
    """Native agy exposes no per-model effort evidence: explicit effort is a
    truthful pre-spawn rejection, never silently dropped."""
    logfile = tmp_path / "agy.log"
    driver = driver_for(tmp_path)
    request = make_request().model_copy(update={"reasoning_effort": "high"})
    events = await collect(driver, request, make_ctx(tmp_path, logfile=logfile))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "unsupported_effort"
    assert session_spawns(logfile) == []


async def test_declared_selectable_effort_reaches_the_cli(tmp_path):
    """A fixture-declared selectable enum maps to the native ``--effort`` flag."""
    logfile = tmp_path / "agy.log"
    driver = driver_for(
        tmp_path,
        effort_fixture={
            MODEL: {"effort": "selectable", "effort_options": ["low", "high"]}
        },
    )
    request = make_request().model_copy(update={"reasoning_effort": "high"})
    events = await collect(driver, request, make_ctx(tmp_path, logfile=logfile))
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    argv = session_spawns(logfile)[0]["argv"]
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--model") + 1] == MODEL
    started = events[0]
    assert started.payload.reasoning_effort == "high"
    assert started.payload.resolved_model == MODEL


async def test_declared_variant_effort_resolves_to_catalog_id(tmp_path):
    """A fixture-declared variant maps the token to an exact catalog id; the
    target is re-checked against the allowlist and the fresh catalog."""
    logfile = tmp_path / "agy.log"
    driver = driver_for(
        tmp_path,
        models=[MODEL, "gemini-3.8-flash-medium"],
        effort_fixture={
            MODEL: {
                "effort": "model_variant",
                "effort_variants": {"medium": "gemini-3.8-flash-medium"},
            }
        },
    )
    request = make_request().model_copy(
        update={"reasoning_effort": "medium", "resolved_model": "gemini-3.8-flash-medium"}
    )
    events = await collect(driver, request, make_ctx(tmp_path, logfile=logfile))
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    argv = session_spawns(logfile)[0]["argv"]
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-medium"
    assert "--effort" not in argv


async def test_variant_target_outside_allowlist_is_rejected(tmp_path):
    """An effort variant can never hop to a model the operator did not admit."""
    logfile = tmp_path / "agy.log"
    driver = driver_for(
        tmp_path,
        effort_fixture={
            MODEL: {
                "effort": "model_variant",
                "effort_variants": {"high": "claude-sonnet-4-6"},
            }
        },
    )
    request = make_request().model_copy(
        update={"reasoning_effort": "high", "resolved_model": "claude-sonnet-4-6"}
    )
    events = await collect(driver, request, make_ctx(tmp_path, logfile=logfile))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "unsupported_effort"
    assert session_spawns(logfile) == []


async def test_effort_option_not_declared_is_rejected(tmp_path):
    logfile = tmp_path / "agy.log"
    driver = driver_for(
        tmp_path,
        effort_fixture={
            MODEL: {"effort": "selectable", "effort_options": ["low"]}
        },
    )
    request = make_request().model_copy(update={"reasoning_effort": "high"})
    events = await collect(driver, request, make_ctx(tmp_path, logfile=logfile))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "unsupported_effort"
    assert session_spawns(logfile) == []
