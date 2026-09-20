"""Devin ACP driver tests. Synthetic fixtures only - no real CLI or account.

Every test spawns the checked-in ``fake_devin`` fixture executable (or an
in-process stub); no production path, credential or network call is exercised.
"""

from __future__ import annotations

import asyncio
import gc
import importlib.metadata
import json
import os
import signal
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

from cli_driver_devin import DevinDriver

FIXTURE = Path(__file__).parent / "fixtures" / "fake_devin.py"
ENTRY_POINT_GROUP = "cli_provider.drivers"
SECRET_THOUGHT = "SECRET-THOUGHT-TEXT"
SECRET_STDERR = "SECRET-STDERR-TOKEN"
SECRET_RPC_ERROR = "SECRET-RPC-ERROR-TEXT"


@pytest.fixture(autouse=True)
async def _collect_subprocess_transports():
    """Collect any lingering subprocess transport while the loop is still open."""
    yield
    gc.collect()


def make_wrapper(tmp_path: Path) -> Path:
    """A real executable whose argv is the fixture script plus the CLI flags."""
    wrapper = tmp_path / "devin"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURE}" "$@"\n', encoding="utf-8"
    )
    wrapper.chmod(0o755)
    return wrapper


def make_ctx(
    tmp_path: Path,
    mode: str,
    *,
    logfile: Path | None = None,
    catalog: str | None = None,
    extra_env: dict[str, str] | None = None,
    permissions=None,
    workspace=None,
) -> RuntimeContext:
    env = dict(os.environ)
    env["FAKE_DEVIN_MODE"] = mode
    if catalog is not None:
        env["FAKE_DEVIN_CATALOG"] = catalog
    if logfile is not None:
        env["FAKE_DEVIN_LOG"] = str(logfile)
    env.update(extra_env or {})
    return RuntimeContext(
        executor=LocalProcessExecutor(env=env),
        permissions=permissions,
        workspace=workspace,
    )


def read_log(logfile: Path) -> list[dict]:
    if not logfile.exists():
        return []
    return [json.loads(line) for line in logfile.read_text().splitlines() if line.strip()]


def make_request(
    *,
    run_id: str = "run_devin_1",
    deadline: float | None = 15.0,
    preset: str = "devin/code",
    model_alias: str | None = None,
) -> NormalizedRequest:
    return NormalizedRequest(
        run_id=run_id,
        task_id="task_devin_1",
        attempt_id="attempt_devin_1",
        preset=preset,
        workspace=WorkspaceRef(workspace_id="ws-alpha"),
        model_alias=model_alias,
        messages=[Message(role="user", content="summarize the module")],
        deadline_seconds=deadline,
    )


def driver_for(tmp_path: Path, **kwargs) -> DevinDriver:
    """Driver configured the way an operator would: fixture CLI + workspace root."""
    return DevinDriver(
        cli_command=str(make_wrapper(tmp_path)),
        workspace_root=str(tmp_path),
        **kwargs,
    )


async def collect(driver: DevinDriver, request: NormalizedRequest, ctx: RuntimeContext) -> list:
    return [event async for event in driver.execute(request, ctx)]


def kinds(events: list) -> list[str]:
    return [event.kind for event in events]


def answer_text(events: list) -> str:
    return "".join(
        event.payload.text for event in events if event.kind == EventKind.MESSAGE_DELTA
    )


def serialized(events: list) -> str:
    return json.dumps([event.model_dump(mode="json") for event in events], ensure_ascii=False)


def terminal(events: list):
    terminals = [
        event
        for event in events
        if event.kind
        in (EventKind.RUN_COMPLETED, EventKind.RUN_FAILED, EventKind.RUN_CANCELLED)
    ]
    assert len(terminals) == 1, f"expected exactly one terminal event, got {kinds(events)}"
    return terminals[0]


class StaticPermissions:
    """Operator-supplied permission policy double: allows only listed actions."""

    def __init__(self, allowed: frozenset[str] | set[str] | None = None) -> None:
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


class RecordingExecutor(LocalProcessExecutor):
    def __init__(self, env) -> None:
        super().__init__(env=env)
        self.calls: list[dict] = []

    async def spawn(self, argv, *, cwd=None):
        self.calls.append({"argv": list(argv), "cwd": cwd})
        return await super().spawn(argv, cwd=cwd)


def recording_ctx(tmp_path: Path, mode: str, **kwargs) -> tuple[RuntimeContext, RecordingExecutor]:
    env = dict(os.environ)
    env["FAKE_DEVIN_MODE"] = mode
    env["FAKE_DEVIN_LOG"] = str(tmp_path / "fixture.log")
    env.update(kwargs.pop("extra_env", {}) or {})
    executor = RecordingExecutor(env)
    return (
        RuntimeContext(executor=executor, permissions=kwargs.get("permissions")),
        executor,
    )


# ----------------------------------------------------------------- packaging


def test_entry_point_is_registered_for_the_runner_allowlist():
    entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    matches = [entry for entry in entry_points if entry.name == "devin"]
    assert matches, "devin driver is not registered in cli_provider.drivers"
    entry = matches[0]
    assert entry.value == "cli_driver_devin:DevinDriver"
    assert entry.dist is not None
    assert entry.dist.metadata["Name"].replace("_", "-").lower() == "cli-driver-devin"
    assert entry.dist.version == "0.1.0"


def test_driver_satisfies_the_sdk_protocol_and_conservative_capabilities():
    driver = DevinDriver()
    assert isinstance(driver, ProviderDriver)
    manifest = driver.manifest
    assert manifest.driver_id == "devin"
    assert manifest.synthetic is False
    assert manifest.sdk_version
    capabilities = driver.capabilities()
    assert capabilities.internal_tools is True
    assert capabilities.external_tool_calls is False
    assert capabilities.vision is False
    assert capabilities.web_search is False
    assert capabilities.structured_output is StructuredOutputMode.NONE
    assert capabilities.usage is UsageProvenance.UNKNOWN
    assert capabilities.sessions is SessionMode.NONE
    assert capabilities.roles is RoleMode.SERIALIZED
    assert capabilities.streaming is StreamingMode.NATIVE
    # accept-edits sessions auto-approve workspace edits: honest capability.
    assert capabilities.workspace_write is True


# --------------------------------------------------------------------- probe


async def test_probe_reports_distribution_version_and_acp_handshake(tmp_path):
    driver = driver_for(tmp_path)
    report = await driver.probe(make_ctx(tmp_path, "ok"))
    assert report.ok is True
    # The ACP agentInfo build string is NOT the distribution version.
    assert report.cli_version == "3000.10.31"
    assert report.cli_version != "0.0.0-dev"
    assert report.capabilities.internal_tools is True


async def test_probe_fails_closed_without_an_executor():
    report = await DevinDriver().probe(RuntimeContext())
    assert report.ok is False
    assert any("no process executor" in note for note in report.notes)


async def test_probe_fails_closed_when_the_version_cannot_be_parsed(tmp_path):
    driver = driver_for(tmp_path)
    report = await driver.probe(make_ctx(tmp_path, "bad_version"))
    assert report.ok is False
    assert report.cli_version is None


async def test_probe_refuses_a_version_that_does_not_match_the_pin(tmp_path):
    driver = driver_for(
        tmp_path, expected_version="9.9.9"
    )
    report = await driver.probe(make_ctx(tmp_path, "ok"))
    assert report.ok is False
    assert report.cli_version == "3000.10.31"


async def test_probe_refuses_an_unexpected_acp_protocol_version(tmp_path):
    driver = driver_for(tmp_path)
    report = await driver.probe(make_ctx(tmp_path, "init_bad_protocol"))
    assert report.ok is False
    assert any("protocol" in note.lower() for note in report.notes)


async def test_probe_fails_on_initialize_rpc_error(tmp_path):
    driver = driver_for(tmp_path)
    report = await driver.probe(make_ctx(tmp_path, "init_error"))
    assert report.ok is False


# ----------------------------------------------------------------- discovery


async def test_discovery_verifies_exact_catalog_membership_and_free_tier(tmp_path):
    driver = driver_for(tmp_path)
    models = await driver.discover_models(make_ctx(tmp_path, "ok"))
    assert [model.model_id for model in models] == ["swe-2-max"]
    verification = models[0].verification
    assert verification.status.value == "passed"
    assert "models list" in verification.source


async def test_discovery_fails_closed_when_model_absent_from_catalog(tmp_path):
    driver = driver_for(tmp_path)
    models = await driver.discover_models(make_ctx(tmp_path, "ok", catalog="missing"))
    assert models[0].verification.status.value == "failed"


async def test_discovery_fails_when_cost_tier_is_not_the_pinned_expectation(tmp_path):
    driver = driver_for(tmp_path)
    models = await driver.discover_models(make_ctx(tmp_path, "ok", catalog="cost"))
    verification = models[0].verification
    assert verification.status.value == "failed"
    assert "cost" in (verification.reason or "").lower()


async def test_discovery_is_unknown_when_the_catalog_cannot_be_read(tmp_path):
    driver = driver_for(tmp_path)
    models = await driver.discover_models(make_ctx(tmp_path, "ok", catalog="bad_json"))
    assert models[0].verification.status.value == "unknown"


async def test_discovery_revalidates_after_the_configured_expiry(tmp_path):
    ctx, executor = recording_ctx(tmp_path, "ok")
    driver = driver_for(tmp_path, catalog_ttl_seconds=0.0)
    await driver.discover_models(ctx)
    await driver.discover_models(ctx)
    catalog_calls = [c for c in executor.calls if "models" in c["argv"]]
    assert len(catalog_calls) >= 2, "an expired verification must not be replayed"

    cached_executor = RecordingExecutor(dict(os.environ, FAKE_DEVIN_MODE="ok"))
    cached_ctx = RuntimeContext(executor=cached_executor)
    cached = driver_for(
        tmp_path, catalog_ttl_seconds=3600.0
    )
    await cached.discover_models(cached_ctx)
    await cached.discover_models(cached_ctx)
    catalog_calls = [c for c in cached_executor.calls if "models" in c["argv"]]
    assert len(catalog_calls) == 1


async def test_discovery_is_unknown_without_an_executor():
    models = await DevinDriver().discover_models(RuntimeContext())
    assert models[0].verification.status.value == "unknown"


# ------------------------------------------------------------------- execute


async def test_full_turn_streams_answer_and_internal_tool_events(tmp_path):
    logfile = tmp_path / "fixture.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "ok", logfile=logfile)
    )
    assert kinds(events)[0] == "run.started"
    assert answer_text(events) == "Hello world"
    assert kinds(events).count(EventKind.TOOL_STARTED) == 1
    assert kinds(events).count(EventKind.TOOL_COMPLETED) == 1
    assert kinds(events).count(EventKind.ARTIFACT_CREATED) == 1
    result = terminal(events)
    assert result.kind == EventKind.RUN_COMPLETED
    assert result.payload.outcome == "succeeded"
    assert result.payload.usage.provenance.value == "unknown"
    # Thought chunks, stderr and RPC error text are never answer/result content.
    blob = serialized(events)
    assert SECRET_THOUGHT not in blob

    seen = [record.get("received") for record in read_log(logfile)]
    assert "initialize" in seen
    assert "session/new" in seen
    assert "session/prompt:begin" in seen
    assert seen.index("initialize") < seen.index("session/new") < seen.index(
        "session/prompt:begin"
    )


async def test_prompt_serializes_roles_as_text_content(tmp_path):
    logfile = tmp_path / "fixture.log"
    driver = driver_for(tmp_path)
    request = make_request()
    request = request.model_copy(
        update={
            "messages": [
                Message(role="system", content="be terse"),
                Message(role="user", content="do the thing"),
            ]
        }
    )
    await collect(driver, request, make_ctx(tmp_path, "ok", logfile=logfile))
    prompts = [r.get("prompt_text") for r in read_log(logfile) if "prompt_text" in r]
    assert prompts and "system: be terse" in prompts[0] and "user: do the thing" in prompts[0]


async def test_usage_update_notifications_never_become_reported_usage(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), make_ctx(tmp_path, "ok"))
    assert EventKind.USAGE_UPDATED not in kinds(events)
    assert terminal(events).payload.usage.provenance.value == "unknown"


async def test_secret_stderr_never_enters_the_event_stream(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), make_ctx(tmp_path, "stderr_noise"))
    assert answer_text(events) == "Hello world"
    assert SECRET_STDERR not in serialized(events)


async def test_acknowledged_session_facts_are_recorded(tmp_path):
    driver = driver_for(tmp_path)
    ctx = make_ctx(tmp_path, "ok")
    await collect(driver, make_request(), ctx)
    facts = await ctx.session_store.get("devin/run_devin_1")
    assert facts is not None
    assert facts["session_id"] == "fixture-session-1"
    assert facts["model"] == "swe-2-max"
    assert facts["mode"] == "accept-edits"


async def test_public_alias_is_never_passed_to_the_cli(tmp_path):
    ctx, executor = recording_ctx(tmp_path, "ok")
    driver = driver_for(tmp_path)
    await collect(
        driver,
        make_request(preset="devin/code", model_alias="swe-2-max"),
        ctx,
    )
    acp_calls = [c["argv"] for c in executor.calls if "acp" in c["argv"]]
    assert acp_calls, "the ACP server was never spawned"
    argv = acp_calls[-1]
    assert argv[argv.index("--model") + 1] == "swe-2-max"
    assert "devin/code" not in argv
    # The fallback/tier environment must be stripped for the child.
    assert "env" == argv[0] or argv[0].endswith("/env")
    joined = " ".join(argv)
    assert "-u DEVIN_REFUSAL_FALLBACK" in joined


async def test_inherited_devin_env_overrides_are_stripped_from_the_child(tmp_path):
    logfile = tmp_path / "fixture.log"
    driver = driver_for(tmp_path)
    ctx = make_ctx(
        tmp_path,
        "ok",
        logfile=logfile,
        extra_env={
            "DEVIN_REFUSAL_FALLBACK": "claude-opus-5-high",
            "DEVIN_MODEL": "claude-opus-5-high",
            "DEVIN_PERMISSION_MODE": "dangerous",
        },
    )
    # Driver-level pins stay operator config; the child must not inherit them.
    events = await collect(driver, make_request(model_alias="swe-2-max"), ctx)
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    env_records = [
        record["env"] for record in read_log(logfile) if record.get("event") == "acp_start"
    ]
    assert env_records, "fixture never logged its environment"
    assert env_records[0]["DEVIN_REFUSAL_FALLBACK"] == "absent"
    assert env_records[0]["DEVIN_MODEL"] == "absent"
    assert env_records[0]["DEVIN_PERMISSION_MODE"] == "absent"


# ---------------------------------------------------------- execute: refusal


async def test_no_executor_and_no_deadline_fail_closed(tmp_path):
    driver = driver_for(tmp_path)
    no_deadline = await collect(
        driver, make_request(deadline=None), make_ctx(tmp_path, "ok")
    )
    assert terminal(no_deadline).payload.code == "no_deadline"
    no_executor = await collect(driver, make_request(), RuntimeContext())
    assert terminal(no_executor).payload.code == "no_process_executor"


async def test_missing_workspace_fails_before_spawn(tmp_path):
    ctx, executor = recording_ctx(tmp_path, "ok")
    driver = DevinDriver(cli_command=str(make_wrapper(tmp_path)))
    events = await collect(driver, make_request(), ctx)
    assert terminal(events).payload.code == "no_workspace"
    assert not any("acp" in c["argv"] for c in executor.calls)


async def test_model_alias_mismatch_is_refused_before_spawn(tmp_path):
    ctx, executor = recording_ctx(tmp_path, "ok")
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(model_alias="swe-2-medium"), ctx)
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "unsupported_model"
    assert not any("acp" in c["argv"] for c in executor.calls)


async def test_effort_suffix_is_an_explicit_unsupported_error_before_spawn(tmp_path):
    ctx, executor = recording_ctx(tmp_path, "ok")
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(model_alias="swe-2-max:maximum"), ctx
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "unsupported_effort"
    assert not any("acp" in c["argv"] for c in executor.calls)


async def test_non_max_configured_model_is_refused_before_spawn(tmp_path):
    ctx, executor = recording_ctx(tmp_path, "ok")
    driver = driver_for(
        tmp_path, model="claude-opus-5-high"
    )
    events = await collect(driver, make_request(), ctx)
    assert terminal(events).payload.code == "unsupported_model"
    assert not any("acp" in c["argv"] for c in executor.calls)


# ------------------------------------------------- execute: catalog re-check


async def test_execute_fails_before_any_prompt_when_membership_is_lost(tmp_path):
    """A stale `Free` listing is never a standing authorization: the catalog
    is re-read on every execute (TTL=0 here) and a dropped model stops the run
    before the agent subprocess exists."""
    logfile = tmp_path / "fixture.log"
    env = dict(os.environ, FAKE_DEVIN_MODE="ok", FAKE_DEVIN_LOG=str(logfile))
    executor = RecordingExecutor(env)
    ctx = RuntimeContext(executor=executor)
    driver = driver_for(tmp_path, catalog_ttl_seconds=0.0)
    events = await collect(driver, make_request(), ctx)
    assert terminal(events).kind == EventKind.RUN_COMPLETED

    executor._env["FAKE_DEVIN_CATALOG"] = "missing"  # provider drops the model
    events = await collect(driver, make_request(run_id="run_devin_lost"), ctx)
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "catalog_not_verified"
    # Denied runs never emit run.started, and no second agent was spawned.
    assert EventKind.RUN_STARTED not in kinds(events)
    acp_calls = [c for c in executor.calls if "acp" in c["argv"]]
    assert len(acp_calls) == 1
    seen = [r.get("received") for r in read_log(logfile)]
    assert seen.count("session/prompt:begin") == 1


async def test_execute_rejects_a_not_free_catalog_before_spawn(tmp_path):
    ctx, executor = recording_ctx(
        tmp_path, "ok", extra_env={"FAKE_DEVIN_CATALOG": "cost"}
    )
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), ctx)
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "catalog_not_verified"
    assert not any("acp" in c["argv"] for c in executor.calls)


async def test_execute_rejects_an_unreadable_catalog_before_spawn(tmp_path):
    ctx, executor = recording_ctx(
        tmp_path, "ok", extra_env={"FAKE_DEVIN_CATALOG": "bad_json"}
    )
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), ctx)
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "catalog_not_verified"
    assert not any("acp" in c["argv"] for c in executor.calls)


async def test_execute_rechecks_the_catalog_within_ttl_bounds(tmp_path):
    """Every run consults the catalog: one read-only `models list` spawn per
    execute when the TTL has expired, and never an agent prompt."""
    ctx, executor = recording_ctx(tmp_path, "ok")
    driver = driver_for(tmp_path, catalog_ttl_seconds=0.0)
    for index in range(2):
        events = await collect(
            driver, make_request(run_id=f"run_d_{index}"), ctx
        )
        assert terminal(events).kind == EventKind.RUN_COMPLETED
    catalog_calls = [c for c in executor.calls if "models" in c["argv"]]
    assert len(catalog_calls) == 2
    for call in catalog_calls:
        assert "acp" not in call["argv"]


async def test_run_started_is_emitted_after_gates_and_before_the_agent(tmp_path):
    # A denied run emits run.failed only — no run.started precedes a refusal.
    ctx, executor = recording_ctx(
        tmp_path, "ok", extra_env={"FAKE_DEVIN_CATALOG": "missing"}
    )
    driver = driver_for(tmp_path, catalog_ttl_seconds=0.0)
    events = await collect(driver, make_request(), ctx)
    assert kinds(events) == ["run.failed"]

    # An admitted run emits run.started first, before the ACP spawn.
    ctx2, executor2 = recording_ctx(tmp_path, "ok")
    events2 = await collect(driver, make_request(run_id="run_d_2"), ctx2)
    assert kinds(events2)[0] == "run.started"
    assert any("acp" in c["argv"] for c in executor2.calls)


# ---------------------------------------------- execute: model/mode pinning


async def test_session_model_mismatch_fails_before_any_prompt(tmp_path):
    logfile = tmp_path / "fixture.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "model_mismatch", logfile=logfile)
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "model_mismatch"
    seen = [record.get("received") for record in read_log(logfile)]
    assert "session/prompt:begin" not in seen


async def test_missing_model_config_fails_before_any_prompt(tmp_path):
    logfile = tmp_path / "fixture.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "no_config_options", logfile=logfile)
    )
    assert terminal(events).kind == EventKind.RUN_FAILED
    seen = [record.get("received") for record in read_log(logfile)]
    assert "session/prompt:begin" not in seen


async def test_bypass_mode_requires_an_explicit_operator_policy(tmp_path):
    logfile = tmp_path / "fixture.log"
    driver = driver_for(
        tmp_path, session_mode="bypass"
    )
    # No permission policy at all: bypass must not be reachable by default.
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "ok", logfile=logfile)
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "mode_not_authorized"
    seen = [record.get("received") for record in read_log(logfile)]
    assert "session/prompt:begin" not in seen

    denying = StaticPermissions()
    logfile.unlink()
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "ok", logfile=logfile, permissions=denying)
    )
    assert terminal(events).payload.code == "mode_not_authorized"
    assert denying.queries, "the policy was never consulted"
    seen = [record.get("received") for record in read_log(logfile)]
    assert "session/prompt:begin" not in seen


async def test_bypass_mode_with_operator_policy_is_acknowledged_then_prompted(tmp_path):
    logfile = tmp_path / "fixture.log"
    policy = StaticPermissions({"devin.acp.session_mode.bypass"})
    driver = driver_for(tmp_path, session_mode="bypass")
    ctx = make_ctx(tmp_path, "ok", logfile=logfile, permissions=policy)
    events = await collect(driver, make_request(), ctx)
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    seen = [record.get("received") for record in read_log(logfile)]
    assert "session/set_mode" in seen
    assert "session/prompt:begin" in seen
    facts = await ctx.session_store.get("devin/run_devin_1")
    assert facts and facts["mode"] == "bypass"


async def test_mode_acknowledgement_mismatch_fails_before_prompt(tmp_path):
    logfile = tmp_path / "fixture.log"
    driver = driver_for(
        tmp_path, session_mode="plan"
    )
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "mode_mismatch", logfile=logfile)
    )
    assert terminal(events).kind == EventKind.RUN_FAILED
    seen = [record.get("received") for record in read_log(logfile)]
    assert "session/set_mode" in seen
    assert "session/prompt:begin" not in seen


async def test_missing_mode_acknowledgement_is_bounded_and_fails(tmp_path):
    logfile = tmp_path / "fixture.log"
    driver = driver_for(
        tmp_path,
        session_mode="plan",
        handshake_timeout_seconds=0.5,
    )
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "mode_no_ack", logfile=logfile)
    )
    assert terminal(events).kind == EventKind.RUN_FAILED
    seen = [record.get("received") for record in read_log(logfile)]
    assert "session/prompt:begin" not in seen


async def test_unavailable_configured_mode_is_refused_before_prompt(tmp_path):
    logfile = tmp_path / "fixture.log"
    driver = driver_for(
        tmp_path, session_mode="does-not-exist"
    )
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "ok", logfile=logfile)
    )
    assert terminal(events).payload.code == "unsupported_mode"
    seen = [record.get("received") for record in read_log(logfile)]
    assert "session/prompt:begin" not in seen


async def test_session_default_differs_is_set_and_acknowledged(tmp_path):
    # The fixture reports currentModeId=plan; the driver default is accept-edits
    # so it must call session/set_mode and wait for the ack before prompting.
    logfile = tmp_path / "fixture.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "current_mode_plan", logfile=logfile)
    )
    assert terminal(events).kind == EventKind.RUN_COMPLETED
    seen = [record.get("received") for record in read_log(logfile)]
    assert "session/set_mode" in seen


# ------------------------------------------------- execute: turn edge cases


async def test_permission_request_is_denied_and_the_run_is_partial(tmp_path):
    logfile = tmp_path / "fixture.log"
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "permission", logfile=logfile)
    )
    assert EventKind.PERMISSION_REQUIRED in kinds(events)
    outcomes = [r.get("permission_outcome") for r in read_log(logfile) if "permission_outcome" in r]
    assert outcomes, "the agent's permission request was never answered"
    outcome = outcomes[0]
    # Denied explicitly: either a selected reject option or the cancelled outcome.
    assert outcome.get("outcome") == "cancelled" or outcome.get("optionId") == "deny"
    result = terminal(events)
    assert result.kind == EventKind.RUN_COMPLETED
    assert result.payload.outcome == "partial"


async def test_malformed_frame_is_a_protocol_error_not_success(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), make_ctx(tmp_path, "malformed"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "protocol_error"


async def test_oversize_frame_is_rejected(tmp_path):
    driver = driver_for(
        tmp_path, max_frame_bytes=4096
    )
    events = await collect(driver, make_request(), make_ctx(tmp_path, "oversize"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "protocol_error"


async def test_eof_mid_turn_is_never_success(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(
        driver, make_request(), make_ctx(tmp_path, "eof_mid_prompt")
    )
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "missing_result"


async def test_unknown_stop_reason_is_a_protocol_error(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), make_ctx(tmp_path, "unknown_stop"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "protocol_error"


async def test_rpc_error_is_failed_and_agent_text_is_not_leaked(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), make_ctx(tmp_path, "rpc_error"))
    result = terminal(events)
    assert result.kind == EventKind.RUN_FAILED
    assert result.payload.code == "rpc_error"
    assert SECRET_RPC_ERROR not in serialized(events)


async def test_session_new_error_is_failed_before_prompt(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), make_ctx(tmp_path, "new_error"))
    assert terminal(events).kind == EventKind.RUN_FAILED


async def test_refusal_and_budget_stops_are_partial_not_failures(tmp_path):
    driver = driver_for(tmp_path)
    for mode in ("refusal", "max_tokens_stop"):
        events = await collect(driver, make_request(), make_ctx(tmp_path, mode))
        result = terminal(events)
        assert result.kind == EventKind.RUN_COMPLETED, mode
        assert result.payload.outcome == "partial", mode


async def test_thought_only_turn_produces_no_answer_delta(tmp_path):
    driver = driver_for(tmp_path)
    events = await collect(driver, make_request(), make_ctx(tmp_path, "thought_only"))
    assert answer_text(events) == ""
    assert terminal(events).kind == EventKind.RUN_COMPLETED


# ----------------------------------------------------------- cancel/timeout


async def test_deadline_cancels_a_hung_prompt_and_kills_the_process(tmp_path):
    driver = driver_for(
        tmp_path, grace_seconds=0.5
    )
    started = time.monotonic()
    events = await collect(
        driver, make_request(deadline=0.8), make_ctx(tmp_path, "hang")
    )
    elapsed = time.monotonic() - started
    assert elapsed < 10.0
    result = terminal(events)
    assert result.kind == EventKind.RUN_CANCELLED
    assert "deadline" in result.payload.reason


async def test_cancel_sends_session_cancel_and_confirms_termination(tmp_path):
    logfile = tmp_path / "fixture.log"
    driver = driver_for(
        tmp_path, grace_seconds=0.5
    )
    request = make_request(deadline=60.0)
    ctx = make_ctx(tmp_path, "hang", logfile=logfile)
    events: list = []

    async def consume() -> None:
        async for event in driver.execute(request, ctx):
            events.append(event)

    task = asyncio.ensure_future(consume())
    for _ in range(200):
        if any(event.kind == EventKind.RUN_STARTED for event in events):
            break
        await asyncio.sleep(0.05)
    assert events, "the run never started"
    # Wait until the prompt is actually in flight so session/cancel has a turn.
    for _ in range(200):
        if any(r.get("received") == "session/prompt:begin" for r in read_log(logfile)):
            break
        await asyncio.sleep(0.05)

    result = await driver.cancel(request.run_id, ctx)
    assert result.requested is True
    assert result.confirmed is True
    await asyncio.wait_for(task, timeout=15)
    assert terminal(events).kind == EventKind.RUN_CANCELLED
    seen = [r.get("received") for r in read_log(logfile)]
    assert "session/cancel" in seen


async def test_cancel_of_an_unknown_run_is_not_confirmed():
    result = await DevinDriver().cancel("run-absent", RuntimeContext())
    assert result.requested is True
    assert result.confirmed is False


# ------------------------------------------------------------ resource use


async def test_repeated_runs_do_not_leak_descriptors_or_processes(tmp_path):
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("descriptor accounting requires /proc")
    driver = driver_for(
        tmp_path, grace_seconds=0.5
    )
    env = dict(os.environ)
    env["FAKE_DEVIN_MODE"] = "ok"
    executor = LocalProcessExecutor(env=env)
    gc.collect()
    gc.disable()
    try:
        before = len(os.listdir("/proc/self/fd"))
        for index in range(5):
            request = make_request(run_id=f"run_leak_{index}")
            events = await collect(
                driver, request, RuntimeContext(executor=executor)
            )
            assert terminal(events).kind == EventKind.RUN_COMPLETED
        after = len(os.listdir("/proc/self/fd"))
    finally:
        gc.enable()
    assert after - before <= 1, f"descriptor growth across runs: {before} -> {after}"
