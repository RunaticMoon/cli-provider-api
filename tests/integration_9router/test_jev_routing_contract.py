"""Opt-in: the real kanban compile+apply path against isolated 9Router 0.5.81.

This test drives ``cli_provider_kanban.compiler.compile_plan``/``apply_plan``
against a REAL 9Router process (temporary HOME/DATA_DIR/port — never the
installed service) fronting the real API + UDS fixture Runner from
``conftest.FixtureSystem``. It proves:

* the compiler emits only eligible members (a disabled/canary candidate is
  dropped, never registered);
* ``apply_plan`` performs the real ``/api/auth/login`` -> ``auth_token``
  cookie session, preflights an empty catalog, writes
  node/provider/combo/settings, and verifies exact readback on the live
  gateway;
* the create-only contract refuses EVERY re-apply variant (identical,
  all-members-disabled, route removed, wrapper URL removed, unrelated
  occupied namespace) BEFORE the first configuration write, with the live
  state byte-for-byte unchanged — a stale route is never reported applied;
* ``WrapperClient`` submits chat through the gateway with the client key and
  reads run control DIRECTLY from the wrapper API with the upstream key —
  task identity, execution metadata, run/attempt ids unchanged;
* a cached replay returns the same run/attempt (body ``cached`` flag, not a
  gateway header);
* a non-operational apply performs zero HTTP calls — proven by pointing the
  target at a dead loopback port: any attempted write would surface a
  transport failure, not the non-operational refusal;
* the ``compile`` CLI dry-run prints the same plan.

Run with NINEROUTER_APP pointing at the pinned 0.5.81 app directory:
    uv run --all-packages pytest tests/integration_9router -v
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest
import yaml

from cli_provider_core import hash_api_key
from cli_provider_kanban.compiler import CompileError, apply_plan, compile_plan
from cli_provider_kanban.policy import load_policy
from cli_provider_kanban.wrapper_client import WrapperClient

ROOT = Path(__file__).resolve().parents[2]
PINNED_VERSION = "0.5.81"
EXECUTION = {
    "task_revision": "rev-9",
    "base_revision": "base-2026.10",
    "route": "worker.code.standard",
    "policy_version": "pol-jev-1",
}


def load_fixture_system():
    path = ROOT / "tests/integration_9router/conftest.py"
    spec = importlib.util.spec_from_file_location(
        "cpa_jev_contract_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.FixtureSystem


def _write_cred(path: Path, payload, mode: int = 0o600) -> str:
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, mode)
    return str(path)


def _policy_dict(gateway_url: str, wrapper_url: str, *, guard: bool) -> dict:
    """A kanban policy whose gateway target is the disposable 9Router and
    whose wrapper is the fixture API. Mirrors conftest.policy_dict shape."""
    return {
        "schema_version": 1,
        "policy_version": "2026-10-01.1",
        "scope": {"assignee": "jev-native", "statuses": ["ready", "todo"]},
        "classifier": {"llm": "disabled"},
        "capabilities": ["code", "review"],
        "decomposition": {"max_depth": 3, "max_children": 8,
                          "replan_cap": 2},
        "limits": {"max_cards": 4},
        "backends": [
            {
                # Disabled+canary API backend: must never reach a combo.
                "id": "bai-flash",
                "kind": "bai",
                "transport": "api",
                "model": "deepseek-v4.1-flash",
                "preset": "bai/deepseek-v4.1-flash",
                "driver": "hermes-api",
                "enabled": False,
                "requires_canary": True,
                "cost_tier": "unknown",
                "disabled_reason": "declared, not yet canary-verified",
                "capabilities": {"code": True},
            },
            {
                "id": "devin-swe-2-max",
                "kind": "devin",
                "transport": "native",
                "model": "swe-2-max",
                "preset": "devin/swe-2-max",
                "driver": "devin",
                "enabled": True,
                "cost_tier": "free",
                "capabilities": {"code": True},
            },
        ],
        "routes": {
            # Disabled member first in policy order — dropped, order preserved.
            "worker.code.standard": {
                "candidates": ["bai-flash", "devin-swe-2-max"],
            },
            "reviewer.review.standard": {
                "candidates": ["bai-flash"],  # held: no eligible member
            },
        },
        "workspaces": {
            "ws-main": {
                "repo": "/nonexistent-trusted",
                "worktree_root": "/nonexistent-trusted/.worktrees",
                "wrapper_workspace_id": "ws-alpha",
            },
        },
        "task_map": None,
        "control": {"operators": ["op-test"]},
        "gateway": {
            "wrapper_base_url": wrapper_url,
            "node_prefix": "jevwrap",
            "targets": [{"name": "gw", "url": gateway_url,
                         "kind": "disposable"}],
            "assume_core_guard": guard,
        },
    }


@pytest.fixture
def jev_gateway(tmp_path):
    """Isolated 9Router + FixtureSystem; yields (gw_url, system, password)."""
    configured = os.environ.get("NINEROUTER_APP")
    if not configured:
        pytest.skip("NINEROUTER_APP missing: real pinned gateway NOT RUN")
    app = Path(configured).resolve()
    package = json.loads((app / "package.json").read_text())
    assert package["name"] == "9router-app"
    assert package["version"] == PINNED_VERSION
    node = os.environ.get("NINEROUTER_NODE") or shutil.which("node")
    assert node, "A trusted Node executable is required"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    home, data, api_dir = (
        tmp_path / name for name in ("home", "router-data", "api"))
    for path in (home, data, api_dir):
        path.mkdir(mode=0o700)
    FixtureSystem = load_fixture_system()
    api_key = secrets.token_urlsafe(32)
    system = FixtureSystem(
        root=str(api_dir), api_key=api_key, plan={"default": "success"},
        config_overrides={
            # The compiled member model is the operator alias; the fixture
            # runner serves it through its discovered synthetic model id.
            "presets": [{
                "alias": "devin/swe-2-max",
                "runner_ref": "runner-1",
                "model_id": "fixture-model",
                "allow_synthetic_unverified": True,
            }],
            "principals": [{
                "name": "alpha",
                "key_hash": hash_api_key(api_key),
                "allowed_presets": ["devin/swe-2-max"],
                "allowed_workspaces": ["ws-alpha"],
                "max_concurrency": 2,
            }],
        },
    )
    password = secrets.token_urlsafe(32)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home), "DATA_DIR": str(data), "PORT": str(port),
        "HOSTNAME": "127.0.0.1", "NODE_ENV": "production",
        "INITIAL_PASSWORD": password, "JWT_SECRET": secrets.token_urlsafe(32),
        "API_KEY_SECRET": secrets.token_urlsafe(32),
        "MACHINE_ID_SALT": "synthetic",
        "MODEL_CATALOG_SYNC": "off", "ENABLE_REQUEST_LOGS": "false",
    }
    log = (tmp_path / "gateway.log").open("wb")
    proc = None
    client = httpx.Client(
        base_url=f"http://127.0.0.1:{port}", timeout=30, trust_env=False)
    try:
        system.start()
        proc = subprocess.Popen(
            [node, str(app / "custom-server.js")], cwd=tmp_path, env=env,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            assert proc.poll() is None, \
                "isolated gateway exited before readiness"
            try:
                if client.get("/api/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("isolated gateway readiness deadline")
        yield client, system, password, api_key
    finally:
        client.close()
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=8)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
            assert proc.poll() is not None
        system.stop()
        log.close()


def test_compile_apply_chat_and_control_through_real_gateway(
        jev_gateway, tmp_path):
    gateway_client, system, password, api_key = jev_gateway
    gateway_url = gateway_client.base_url
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        yaml.safe_dump(_policy_dict(
            str(gateway_url).rstrip("/"), system.base_url, guard=True)),
        encoding="utf-8",
    )
    policy = load_policy(policy_path)

    # The CLI entrypoint renders the identical plan — exercise it for real.
    cli = subprocess.run(
        [sys.executable, "-m", "cli_provider_kanban", "compile",
         "--policy", str(policy_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert cli.returncode == 0, cli.stderr
    cli_plan = json.loads(cli.stdout)
    plan = compile_plan(policy)
    assert cli_plan["combos"] == plan["combos"]

    std = next(c for c in plan["combos"]
               if c["name"] == "jev.worker.code.standard")
    # Only the eligible member is registered; the disabled canary is dropped.
    assert std["models"] == ["jevwrap/devin/swe-2-max"]
    assert std["operational"] is True
    held = next(c for c in plan["combos"]
                if c["name"] == "jev.reviewer.review.standard")
    assert held["models"] == [] and held["held"] is True

    # Apply over the REAL management contract: password -> auth_token cookie.
    cred = _write_cred(tmp_path / "gw-cred.json", {
        "upstream_key": api_key,
        "management_password": password,
    })
    out = apply_plan(policy, target_name="gw", credential_file=cred)
    assert out["readback"] == "verified"
    assert out["held"] == ["jev.reviewer.review.standard"]

    # A separate dashboard session for inspection (the compiler's session was
    # its own). Login enables the cookie jar for management reads below.
    assert gateway_client.post(
        "/api/auth/login", json={"password": password}).status_code == 200

    # The compiled state is actually live on the gateway.
    combos = gateway_client.get("/api/combos").json()
    values = combos if isinstance(combos, list) else combos["combos"]
    live = next(c for c in values
                if c["name"] == "jev.worker.code.standard")
    assert live["models"] == ["jevwrap/devin/swe-2-max"]

    # A 9Router client key for chat submission — distinct from both the
    # management session and the upstream API key.
    key = gateway_client.post("/api/keys", json={"name": "jev-contract"})
    assert key.status_code == 201
    client_key_file = _write_cred(tmp_path / "client-key", key.json()["key"])
    upstream_key_file = _write_cred(tmp_path / "upstream-key", api_key)

    client = WrapperClient(
        str(gateway_url).rstrip("/"),
        credential_file=client_key_file,
        control_base_url=system.base_url,
        control_credential_file=upstream_key_file,
        timeout_seconds=30.0,
    )
    out1 = client.submit_chat(
        model="jev.worker.code.standard",   # the compiled combo name
        task_id="jev-contract-task",
        workspace_id="ws-alpha",
        messages=[{"role": "user", "content": "synthetic task"}],
        execution=EXECUTION,
    )
    assert out1.status == "completed"
    run = out1.run
    # Task identity, execution metadata and ids unchanged end to end.
    assert run["task_id"] == "jev-contract-task"
    assert run["workspace_id"] == "ws-alpha"
    received = system.runs_received()
    assert len(received) == 1
    assert received[0]["task_id"] == "jev-contract-task"
    assert received[0]["execution"] == EXECUTION
    assert received[0]["run_id"] == out1.run_id
    assert len(system.effects()) == 1

    # Direct control read against the wrapper API with the upstream key.
    view = client.get_run(out1.run_id)
    assert view["run_id"] == out1.run_id
    assert view["attempt_id"] == out1.attempt_id
    assert view["task_id"] == "jev-contract-task"
    assert view["execution"] == EXECUTION
    assert view["status"] == "completed"

    # Cached replay — same run, same attempt, no second execution. The body
    # flag is the authority even if the gateway drops X-Run-Cached.
    out2 = client.submit_chat(
        model="jev.worker.code.standard",
        task_id="jev-contract-task",
        workspace_id="ws-alpha",
        messages=[{"role": "user", "content": "synthetic task"}],
        execution=EXECUTION,
    )
    assert out2.cached is True
    assert out2.run_id == out1.run_id
    assert out2.attempt_id == out1.attempt_id
    assert len(system.runs_received()) == 1
    assert len(system.effects()) == 1


def _catalog_snapshot(gateway_client) -> dict:
    """Exact management state for before/after comparison — combos, nodes,
    connections (by canonical id) and the global settings keys apply owns."""
    def entries(path, key):
        body = gateway_client.get(path).json()
        values = body if isinstance(body, list) else body.get(key)
        return values or []

    settings = gateway_client.get("/api/settings").json()
    return {
        "combos": sorted(
            json.dumps({k: c.get(k) for k in ("name", "models")},
                       sort_keys=True)
            for c in entries("/api/combos", "combos")),
        "nodes": sorted(
            json.dumps({k: n.get(k) for k in ("id", "name", "prefix",
                                              "baseUrl")}, sort_keys=True)
            for n in entries("/api/provider-nodes", "nodes")),
        "connections": sorted(
            json.dumps({k: c.get(k) for k in ("id", "name", "provider")},
                       sort_keys=True)
            for c in entries("/api/providers", "connections")),
        "settings": {
            k: settings.get(k)
            for k in ("comboStrategy", "fallbackStrategy")
        },
    }


def test_reapply_variants_refuse_before_any_write(jev_gateway, tmp_path):
    """BUG-1/BUG-2 against the real pinned gateway: once a target is
    configured, EVERY re-apply variant refuses at preflight — identical
    plan, all members disabled (route became held), route removed, and
    wrapper URL removed — BEFORE any settings/node/provider/combo write,
    with the live state byte-for-byte unchanged afterwards. The stale
    combo is never claimed applied, disabled, or reconciled away."""
    gateway_client, system, password, api_key = jev_gateway
    gateway_url = str(gateway_client.base_url).rstrip("/")
    cred = _write_cred(tmp_path / "gw-cred.json", {
        "upstream_key": api_key,
        "management_password": password,
    })
    base = _policy_dict(gateway_url, system.base_url, guard=True)
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    out = apply_plan(load_policy(policy_path), target_name="gw",
                     credential_file=cred)
    assert out["readback"] == "verified"

    # Independent dashboard session for state inspection.
    assert gateway_client.post(
        "/api/auth/login", json={"password": password}).status_code == 200
    # Retune a global setting the plan would otherwise overwrite — proves
    # the refusal happens before PATCH /api/settings, not after it.
    assert gateway_client.patch(
        "/api/settings", json={"comboStrategy": "priority"}
    ).status_code == 200
    before = _catalog_snapshot(gateway_client)
    assert before["settings"]["comboStrategy"] == "priority"

    variants = [dict(base)]  # identical re-apply
    all_disabled = json.loads(json.dumps(base))
    for backend in all_disabled["backends"]:
        backend["enabled"] = False          # every route becomes held
    variants.append(all_disabled)
    route_removed = json.loads(json.dumps(base))
    route_removed["routes"].pop("worker.code.standard")
    variants.append(route_removed)
    wrapper_removed = json.loads(json.dumps(base))
    wrapper_removed["gateway"]["wrapper_base_url"] = None
    variants.append(wrapper_removed)

    for i, variant in enumerate(variants):
        path = tmp_path / f"policy-v{i}.yaml"
        path.write_text(yaml.safe_dump(variant), encoding="utf-8")
        with pytest.raises(CompileError, match="occupied"):
            apply_plan(load_policy(path), target_name="gw",
                       credential_file=cred)
        assert _catalog_snapshot(gateway_client) == before
    # Exactly one node, one connection, one combo — no duplicates (BUG-2
    # evidence was 1 -> 2 on re-apply).
    assert len(before["nodes"]) == 1
    assert len(before["connections"]) == 1
    assert any(
        json.loads(c)["name"] == "jev.worker.code.standard"
        for c in before["combos"])


def test_unrelated_occupied_gateway_refuses_before_writes(
        jev_gateway, tmp_path):
    """An unrelated namespace counts too: PATCH /api/settings is global and
    would silently retune foreign combos, so a NON-empty target is refused
    wholesale — the disposable contract is 'fresh', not 'jev.*-free'."""
    gateway_client, system, password, api_key = jev_gateway
    assert gateway_client.post(
        "/api/auth/login", json={"password": password}).status_code == 200
    foreign = gateway_client.post("/api/combos", json={
        "name": "other.team.combo", "models": ["someone/else"]})
    assert foreign.status_code in (200, 201)
    before = _catalog_snapshot(gateway_client)

    gateway_url = str(gateway_client.base_url).rstrip("/")
    cred = _write_cred(tmp_path / "gw-cred.json", {
        "upstream_key": api_key,
        "management_password": password,
    })
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        yaml.safe_dump(_policy_dict(gateway_url, system.base_url,
                                    guard=True)),
        encoding="utf-8")
    with pytest.raises(CompileError, match="occupied"):
        apply_plan(load_policy(policy_path), target_name="gw",
                   credential_file=cred)
    assert _catalog_snapshot(gateway_client) == before


def test_unsafe_apply_makes_zero_http_calls(jev_gateway, tmp_path):
    """assume_core_guard unattested -> every membered combo is
    non-operational; apply refuses BEFORE any HTTP write. The target is a
    dead loopback port: a single attempted call would surface a transport
    failure instead of the non-operational refusal."""
    gateway_client, system, password, api_key = jev_gateway
    assert gateway_client.post(
        "/api/auth/login", json={"password": password}).status_code == 200
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        dead_port = reservation.getsockname()[1]
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        yaml.safe_dump(_policy_dict(
            f"http://127.0.0.1:{dead_port}", system.base_url, guard=False)),
        encoding="utf-8",
    )
    policy = load_policy(policy_path)
    cred = _write_cred(tmp_path / "gw-cred.json", {
        "upstream_key": api_key,
        "management_password": "irrelevant-dead-target",
    })
    with pytest.raises(CompileError, match="non-operational"):
        apply_plan(policy, target_name="gw", credential_file=cred)
    # Belt and braces: the live gateway carries no jev.* combos at all.
    combos = gateway_client.get("/api/combos").json()
    values = combos if isinstance(combos, list) else combos["combos"]
    assert not any(str(c.get("name", "")).startswith("jev.")
                   for c in values)
