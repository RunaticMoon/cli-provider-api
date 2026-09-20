"""Real-Hermes fixture integration: installed CLI against a loopback mock.

These tests spawn the actually-installed ``hermes`` binary and pin the selected
official provider route at a local scripted OpenAI-compatible server. No
account, no external network, no live secret file: keys are synthetic and the
provider endpoint is 127.0.0.1. The driver under test still builds the real
task-local HERMES_HOME, generated config, argv and event mapping.

Marked ``hermes_integration``; skipped when the binary is absent.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cli_provider_sdk import (
    EventKind,
    Message,
    NormalizedRequest,
    RuntimeContext,
    WorkspaceRef,
)
from cli_provider_transports import LocalProcessExecutor

from cli_driver_hermes_api import HermesApiDriver, PRESETS, ProviderPreset

FIXTURES = Path(__file__).parent / "fixtures"
MOCK = FIXTURES / "openai_mock.py"

HERMES_BIN = os.environ.get("HERMES_API_TEST_CLI") or shutil.which("hermes") \
    or "/home/ubuntu/.local/bin/hermes"

pytestmark = pytest.mark.skipif(
    not Path(HERMES_BIN).exists(), reason="hermes CLI not installed"
)

BAI_PRESET = "bai/deepseek-v4.1-flash"
CC_PRESET = "commandcode/deepseek-v4.1-flash"
BAI_KEY = "sk-test-bai-fixture"
CC_KEY = "sk-test-cc-fixture"


class AllowAll:
    def allows(self, action: str) -> bool:
        return True


class TmpWorkspace:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> str:
        return str(self._root)

    def resolve(self, relative: str) -> str:
        return os.path.join(self._root, relative)


def start_mock(tmp_path: Path) -> tuple[subprocess.Popen, str, Path]:
    """Spawn the loopback mock on an ephemeral port; return (proc, base_url)."""
    log = tmp_path / "provider-wire.jsonl"
    # Pick a free port by binding then releasing; the mock rebinds immediately.
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    env = dict(os.environ)
    env["HERMES_MOCK_LOG"] = str(log)
    proc = subprocess.Popen([sys.executable, str(MOCK), str(port)], env=env)
    for _ in range(100):
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            sock.close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        proc.kill()
        raise RuntimeError("mock provider did not start")
    return proc, f"http://127.0.0.1:{port}", log


def wire_requests(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def chat_requests(log: Path) -> list[dict]:
    return [r for r in wire_requests(log) if r.get("_path", "").endswith("/chat/completions")]


def make_ctx(tmp_path: Path, workspace: Path) -> RuntimeContext:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "BAI_API_KEY": BAI_KEY,
        "COMMANDCODE_API_KEY": CC_KEY,
        "LANG": "C.UTF-8",
    }
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "input.txt").write_text("fixture-input\n", encoding="utf-8")
    return RuntimeContext(
        executor=LocalProcessExecutor(env=env),
        workspace=TmpWorkspace(workspace),
        permissions=AllowAll(),
    )


def make_request(preset: str, run_id: str = "run_int_1") -> NormalizedRequest:
    return NormalizedRequest(
        run_id=run_id,
        task_id="task_int_1",
        attempt_id="attempt_int_1",
        preset=preset,
        workspace=WorkspaceRef(workspace_id="ws-int"),
        messages=[Message(role="user", content=(
            "Read input.txt, then write agent-out.txt containing "
            "fixture-agent-wrote-this, then answer briefly."))],
        deadline_seconds=180.0,
    )


def driver_with_mock(preset_alias: str, base_url: str, tmp_path: Path) -> HermesApiDriver:
    preset = PRESETS[preset_alias]
    mocked = ProviderPreset(
        alias=preset.alias, provider_name=preset.provider_name,
        provider_kind=preset.provider_kind, model_id=preset.model_id,
        base_url=f"{base_url}{preset.base_path}", base_path=preset.base_path,
        api_mode=preset.api_mode, key_env=preset.key_env,
    )
    return HermesApiDriver(
        cli_command=HERMES_BIN,
        presets={preset_alias: mocked},
        reasoning="high",
        state_dir=str(tmp_path / "state"),
        run_budget_cap=120,
        max_turns=8,
    )


@pytest.mark.parametrize("preset_alias", [BAI_PRESET, CC_PRESET])
async def test_real_hermes_against_loopback_provider(tmp_path, preset_alias):
    """End-to-end: pinned model+provider, real file writes, wire assertions."""
    mock, base_url, log = start_mock(tmp_path)
    try:
        workspace = tmp_path / "ws"
        ctx = make_ctx(tmp_path, workspace)
        driver = driver_with_mock(preset_alias, base_url, tmp_path)
        events = [e async for e in driver.execute(make_request(preset_alias), ctx)]
    finally:
        mock.terminate()
        try:
            mock.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mock.kill()

    # Event stream: completed run, answer text present, tool lifecycle seen.
    kinds = [e.kind for e in events]
    assert kinds[-1] == EventKind.RUN_COMPLETED, kinds
    deltas = "".join(e.payload.text for e in events
                     if e.kind == EventKind.MESSAGE_DELTA)
    assert "fixture final answer" in deltas
    assert EventKind.TOOL_STARTED in kinds and EventKind.TOOL_COMPLETED in kinds

    # Actual isolated file change happened inside the workspace.
    assert (workspace / "agent-out.txt").read_text() == "fixture-agent-wrote-this"

    chats = chat_requests(log)
    assert len(chats) >= 3, [r.get("_path") for r in wire_requests(log)]
    preset = PRESETS[preset_alias]
    for request in chats:
        # Exact pinned model on every call; no fallback or reclassification.
        assert request["model"] == preset.model_id
        assert request["reasoning_effort"] == "high"
    # Tool history: second+ requests carry tool-result messages, and assistant
    # tool-call turns keep the DeepSeek reasoning_content echo.
    later = chats[1:]
    assert any(m.get("role") == "tool" for r in later for m in r["messages"])
    echoed = [
        m for r in later for m in r["messages"]
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert echoed, "no assistant tool-call turn was replayed"
    assert all("reasoning_content" in m for m in echoed)

    # Secrets never appear in wire logs as config values, argv or events.
    key = BAI_KEY if preset_alias == BAI_PRESET else CC_KEY
    serialized_events = json.dumps([e.model_dump(mode="json") for e in events])
    assert key not in serialized_events


async def test_probe_against_installed_hermes(tmp_path):
    driver = HermesApiDriver(cli_command=HERMES_BIN)
    report = await driver.probe(
        RuntimeContext(executor=LocalProcessExecutor(env=dict(os.environ))))
    assert report.ok is True
    assert report.cli_version == "0.21.3"
