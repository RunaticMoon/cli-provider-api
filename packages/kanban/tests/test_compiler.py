"""Compiler tests — pure plan rendering plus apply against a stub 9Router
management API on loopback (the only target kind this slice permits).

The stub mirrors the real 0.5.81 management contract exercised in
tests/integration_9router: ``POST /api/auth/login`` issues an ``auth_token``
session cookie; every other ``/api/*`` call requires that cookie; provider
nodes/providers/combos are created with POST and read back with GET. The stub
records every call so fail-closed apply tests can prove zero HTTP writes.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from cli_provider_kanban.compiler import (
    CompileError,
    apply_plan,
    compile_plan,
)
from cli_provider_kanban.policy import load_policy

from conftest import policy_dict as _base_policy_dict, write_policy


def policy_dict(**overrides) -> dict:
    """The shared fixture policy with CORRECTED driver-pinned preset aliases.

    The checked-in example still carries the invented ``hermes-api/bai-*``
    names (reported to the parent as a required example edit); the real
    hermes-api driver only accepts ``bai/deepseek-v4.1-flash`` and
    ``commandcode/deepseek-v4.1-flash``.
    """
    data = _base_policy_dict(**overrides)
    for backend in data["backends"]:
        if backend["driver"] == "hermes-api" and backend["kind"] == "bai":
            backend["preset"] = "bai/deepseek-v4.1-flash"
        elif backend["driver"] == "hermes-api" and backend["kind"] == "commandcode":
            backend["preset"] = "commandcode/deepseek-v4.1-flash"
    return data


def _write_cred(tmp_path: Path, data: dict, mode: int = 0o600,
                name: str = "cred.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, mode)
    return str(path)


@pytest.fixture
def policy(tmp_path):
    data = policy_dict()
    data["gateway"] = {
        "wrapper_base_url": "http://127.0.0.1:8080",
        "node_prefix": "jevwrap",
        "targets": [
            {"name": "local", "url": "http://127.0.0.1:9",
             "kind": "disposable"},
            {"name": "prod", "url": "https://router.example.com",
             "kind": "production"},
        ],
    }
    return load_policy(write_policy(tmp_path, data))


class TestCompilePlan:
    def test_combos_carry_only_eligible_members(self, policy):
        plan = compile_plan(policy)
        combos = {c["name"]: c for c in plan["combos"]}
        # bai-flash is disabled+canary -> not a registered member anywhere.
        # Policy order is preserved for the members that are eligible.
        assert combos["jev.worker.code.easy"]["models"] == [
            "jevwrap/devin/swe-2-max",
        ]
        assert combos["jev.worker.code.standard"]["models"] == [
            "jevwrap/devin/swe-2-max",
        ]
        for combo in combos.values():
            for member in combo["models"]:
                assert "bai" not in member and "commandcode" not in member

    def test_ineligible_members_recorded_as_dropped(self, policy):
        plan = compile_plan(policy)
        combos = {c["name"]: c for c in plan["combos"]}
        dropped = combos["jev.worker.code.easy"]["dropped"]
        assert dropped == [
            {"backend": "bai-flash",
             "reason": "backend disabled (bai-flash: B.AI official API "
                       "authorized by correction 2444; kept disabled until a "
                       "real canary proves the wired path. Verified model id "
                       "deepseek-v4.1-flash at https://api.b.ai/v1.)"},
        ] or [d["backend"] for d in dropped] == ["bai-flash"]

    def test_enabled_canary_verified_member_is_compiled(self, tmp_path):
        data = policy_dict()
        for backend in data["backends"]:
            if backend["id"] == "bai-flash":
                backend["enabled"] = True
                backend["requires_canary"] = False
        data["gateway"] = {"wrapper_base_url": "http://127.0.0.1:8080",
                           "node_prefix": "jevwrap", "targets": [],
                           "assume_core_guard": True}
        policy = load_policy(write_policy(tmp_path, data))
        plan = compile_plan(policy)
        combos = {c["name"]: c for c in plan["combos"]}
        # easy: BAI -> Devin order preserved with real driver aliases.
        assert combos["jev.worker.code.easy"]["models"] == [
            "jevwrap/bai/deepseek-v4.1-flash",
            "jevwrap/devin/swe-2-max",
        ]
        assert combos["jev.worker.code.standard"]["models"] == [
            "jevwrap/devin/swe-2-max",
            "jevwrap/bai/deepseek-v4.1-flash",
        ]
        assert combos["jev.worker.code.easy"]["operational"] is True

    def test_empty_route_is_held(self, policy):
        plan = compile_plan(policy)
        combos = {c["name"]: c for c in plan["combos"]}
        held = combos["jev.reviewer.review.standard"]
        assert held["held"] is True
        assert held["models"] == []
        assert held["operational"] is False
        assert [d["backend"] for d in held["dropped"]] == ["devin-opus-review"]

    def test_core_guard_required_for_every_tier(self, tmp_path):
        # easy/free are effectful too (native agent + API tool loop) — the
        # no-post-dispatch-retry guard attestation is not a difficulty check.
        for route_key in ("worker.code.easy", "worker.code.standard"):
            data = policy_dict()
            data["routes"] = {route_key: {"candidates": ["devin-swe-2-max"]}}
            data["gateway"] = {
                "wrapper_base_url": "http://127.0.0.1:8080",
                "node_prefix": "jevwrap", "targets": [],
                "assume_core_guard": False,
            }
            policy = load_policy(write_policy(tmp_path, data))
            combo = compile_plan(policy)["combos"][0]
            assert combo["models"] == ["jevwrap/devin/swe-2-max"]
            assert combo["operational"] is False

            data["gateway"]["assume_core_guard"] = True
            policy2 = load_policy(write_policy(tmp_path, data, "p2.yaml"))
            assert compile_plan(policy2)["combos"][0]["operational"] is True

    def test_enabled_backend_with_unverifiable_preset_fails(self, tmp_path):
        """An enabled+routed member whose preset is not driver-pinned is a
        hard CompileError — the invented hermes-api/bai-* alias would fail at
        the driver anyway; the compiler must say so first."""
        data = policy_dict()
        for backend in data["backends"]:
            if backend["id"] == "bai-flash":
                backend["enabled"] = True
                backend["requires_canary"] = False
                backend["preset"] = "hermes-api/bai-deepseek-v4.1-flash"
        data["routes"] = {"worker.code.easy": {"candidates": ["bai-flash"]}}
        data["gateway"] = {"wrapper_base_url": "http://127.0.0.1:8080",
                           "node_prefix": "jevwrap", "targets": []}
        policy = load_policy(write_policy(tmp_path, data))
        with pytest.raises(CompileError, match="bai-flash") as exc:
            compile_plan(policy)
        assert "bai/deepseek-v4.1-flash" in str(exc.value)

    def test_enabled_backend_model_mismatch_fails(self, tmp_path):
        data = policy_dict()
        for backend in data["backends"]:
            if backend["id"] == "bai-flash":
                backend["enabled"] = True
                backend["requires_canary"] = False
                backend["model"] = "deepseek-v9.9"  # not the pinned model id
        data["routes"] = {"worker.code.easy": {"candidates": ["bai-flash"]}}
        data["gateway"] = {"wrapper_base_url": "http://127.0.0.1:8080",
                           "node_prefix": "jevwrap", "targets": []}
        policy = load_policy(write_policy(tmp_path, data))
        with pytest.raises(CompileError, match="model"):
            compile_plan(policy)

    def test_enabled_backend_unknown_driver_fails(self, tmp_path):
        data = policy_dict()
        for backend in data["backends"]:
            if backend["id"] == "bai-flash":
                backend["enabled"] = True
                backend["requires_canary"] = False
                backend["driver"] = "mystery-driver"
        data["routes"] = {"worker.code.easy": {"candidates": ["bai-flash"]}}
        data["gateway"] = {"wrapper_base_url": "http://127.0.0.1:8080",
                           "node_prefix": "jevwrap", "targets": []}
        policy = load_policy(write_policy(tmp_path, data))
        with pytest.raises(CompileError, match="mystery-driver"):
            compile_plan(policy)

    def test_enabled_devin_unsupported_model_fails(self, tmp_path):
        data = policy_dict()
        for backend in data["backends"]:
            if backend["id"] == "devin-swe-2-max":
                backend["model"] = "claude-opus-5-high"
                backend["preset"] = "devin/claude-opus-5-high"
        data["routes"] = {
            "worker.code.easy": {"candidates": ["devin-swe-2-max"]},
        }
        data["gateway"] = {"wrapper_base_url": "http://127.0.0.1:8080",
                           "node_prefix": "jevwrap", "targets": []}
        policy = load_policy(write_policy(tmp_path, data))
        with pytest.raises(CompileError, match="swe-2-max"):
            compile_plan(policy)

    def test_disabled_bad_contract_is_held_not_error(self, tmp_path):
        """A disabled member with a bad preset is held (visible in dropped),
        never a compile failure — it is never registered as active."""
        data = policy_dict()
        for backend in data["backends"]:
            if backend["id"] == "bai-flash":
                backend["preset"] = "hermes-api/bai-deepseek-v4.1-flash"
        data["gateway"] = {"wrapper_base_url": "http://127.0.0.1:8080",
                           "node_prefix": "jevwrap", "targets": []}
        policy = load_policy(write_policy(tmp_path, data))
        plan = compile_plan(policy)  # must not raise
        easy = {c["name"]: c for c in plan["combos"]}["jev.worker.code.easy"]
        assert all("bai" not in m for m in easy["models"])

    def test_plan_is_secret_free(self, policy, tmp_path):
        _write_cred(tmp_path, {"management_password": "SECRET-MGMT",
                               "upstream_key": "SECRET-UP"})
        plan = compile_plan(policy)
        blob = json.dumps(plan)
        assert "SECRET" not in blob
        assert "apiKey" not in blob or "<credential file" in blob

    def test_no_codex_anywhere(self, policy):
        blob = json.dumps(compile_plan(policy)).lower()
        assert "codex" not in blob

    def test_provider_node_targets_wrapper(self, policy):
        plan = compile_plan(policy)
        node = plan["provider_nodes"][0]
        assert node["baseUrl"] == "http://127.0.0.1:8080/v1"
        assert node["type"] == "openai-compatible"

    def test_single_provider_for_all_candidates(self, tmp_path):
        """One provider node + one provider key covers every candidate —
        no fallback is duplicated inside Jev."""
        data = policy_dict()
        for backend in data["backends"]:
            if backend["id"] == "bai-flash":
                backend["enabled"] = True
                backend["requires_canary"] = False
        data["gateway"] = {"wrapper_base_url": "http://127.0.0.1:8080",
                           "node_prefix": "jevwrap", "targets": [],
                           "assume_core_guard": True}
        policy = load_policy(write_policy(tmp_path, data))
        plan = compile_plan(policy)
        assert len(plan["provider_nodes"]) == 1
        assert len(plan["providers"]) == 1

    def test_presets_advisory_without_runner_map(self, policy):
        plan = compile_plan(policy)
        # No operator runner mapping -> NOTHING may pretend to be OperatorConfig.
        assert plan["presets"] == []
        advisory = {p["alias"]: p for p in plan["presets_advisory"]}
        entry = advisory["bai/deepseek-v4.1-flash"]
        assert "runner_ref" not in entry
        assert entry["model_id"] == "bai:deepseek-v4.1-flash"
        assert entry["advisory"]

    def test_presets_loadable_with_runner_map(self, tmp_path):
        """With an operator driver->runner mapping the fragment is real
        PresetConfig — proven by loading it into a toy OperatorConfig."""
        from cli_provider_core.config import OperatorConfig
        from cli_provider_core.hashing import hash_api_key

        data = policy_dict()
        for backend in data["backends"]:
            if backend["id"] == "bai-flash":
                backend["enabled"] = True
                backend["requires_canary"] = False
        data["gateway"] = {"wrapper_base_url": "http://127.0.0.1:8080",
                           "node_prefix": "jevwrap", "targets": []}
        policy = load_policy(write_policy(tmp_path, data))
        plan = compile_plan(
            policy, runner_map={"devin": "runner-1", "hermes-api": "runner-1"}
        )
        # Only contract-provable entries are loadable — devin/claude-opus-5-high
        # is outside the devin driver's supported set and lands in advisory.
        aliases = {p["alias"] for p in plan["presets"]}
        assert aliases == {
            "devin/swe-2-max",
            "bai/deepseek-v4.1-flash",
            "commandcode/deepseek-v4.1-flash",
        }
        by_alias = {p["alias"]: p for p in plan["presets"]}
        assert by_alias["bai/deepseek-v4.1-flash"]["model_id"] == \
            "bai:deepseek-v4.1-flash"
        assert by_alias["commandcode/deepseek-v4.1-flash"]["model_id"] == \
            "commandcode:deepseek-v4.1-flash"
        assert by_alias["devin/swe-2-max"]["model_id"] == "swe-2-max"
        assert {p["alias"] for p in plan["presets_advisory"]} == \
            {"devin/claude-opus-5-high"}
        # The toy OperatorConfig — this is the full loadable shape.
        toy = {
            "schema_version": 1,
            "data_dir": "/tmp/toy-data",
            "runners": [{
                "instance_id": "runner-1", "driver_id": "hermes-api",
                "driver_version": "0.1.0",
                "distribution": "cli-driver-hermes-api",
                "socket_path": "/tmp/toy.sock",
            }],
            "presets": plan["presets"],
            "workspaces": [{"workspace_id": "ws-alpha"}],
            "principals": [{
                "name": "toy",
                "key_hash": hash_api_key("toy-key"),
                "allowed_presets": sorted(aliases),
                "allowed_workspaces": ["ws-alpha"],
            }],
        }
        cfg = OperatorConfig.model_validate(toy)
        assert cfg.preset_map()["bai/deepseek-v4.1-flash"].runner_ref == \
            "runner-1"

    def test_effort_is_intent_never_wire(self, policy):
        plan = compile_plan(policy)
        effort = plan["effort"]
        assert effort["applied_hints"] == ["auto"]
        # Verified mapping preserved as intent — never a 9Router parameter.
        assert effort["intent"]["bai-flash"]["balanced"] == "high"
        blob = json.dumps(plan["combos"])
        assert "effort" not in blob


class StubRouter:
    """Stub 9Router management API on loopback — the REAL 0.5.81 contract:

    ``POST /api/auth/login`` {"password": ...} -> 200 + Set-Cookie
    ``auth_token=<session>``; every other ``/api/*`` requires that cookie.
    Providers never echo apiKey on GET (like the real app).
    """

    PASSWORD = "stub-mgmt-password"
    COOKIE = "stub-session-cookie"

    def __init__(self):
        import http.server
        server = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def _send(self, code, obj, headers=None):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def _authed(self):
                cookies = self.headers.get("Cookie") or ""
                return StubRouter.COOKIE in cookies

            def do_POST(self):
                body = self._body()
                if self.path == "/api/auth/login":
                    server.calls.append(("POST", self.path, {"password": "***"}))
                    if body.get("password") == StubRouter.PASSWORD:
                        self._send(200, {"ok": True}, {
                            "Set-Cookie": f"auth_token={StubRouter.COOKIE}; "
                                          "HttpOnly; Path=/"})
                    else:
                        self._send(401, {"error": "Invalid password."})
                    return
                server.calls.append(("POST", self.path, body))
                if not self._authed():
                    self._send(401, {"error": "Unauthorized"})
                    return
                if self.path == server.error_path:
                    self._send(500, {"error": {"detail": server.error_secret}})
                elif self.path == "/api/provider-nodes":
                    nid = f"node_{len(server.nodes)+1}"
                    server.nodes[body["name"]] = {"id": nid, **body}
                    self._send(201, {"node": server.nodes[body["name"]]})
                elif self.path == "/api/providers":
                    conn = dict(body)
                    conn.pop("apiKey", None)  # the real app never echoes it
                    server.providers.append(conn)
                    self._send(201, {"connection": conn})
                elif self.path == "/api/combos":
                    server.combos.append({"name": body["name"],
                                          "models": body["models"]})
                    self._send(201, {"combo": server.combos[-1]})
                elif self.path == "/api/keys":
                    self._send(201, {"key": "stub-client-key"})
                else:
                    self._send(404, {"error": "nope"})

            def do_PATCH(self):
                if not self._authed():
                    server.calls.append(("PATCH", self.path, "UNAUTHED"))
                    self._send(401, {"error": "Unauthorized"})
                    return
                body = self._body()
                server.calls.append(("PATCH", self.path, body))
                if self.path == "/api/settings":
                    server.settings.update(body)
                    self._send(200, dict(server.settings))
                else:
                    self._send(404, {"error": "nope"})

            def do_GET(self):
                server.calls.append(("GET", self.path, None))
                if not self._authed():
                    self._send(401, {"error": "Unauthorized"})
                    return
                if self.path == "/api/combos":
                    payload = (server.readback_override
                               if server.readback_override is not None
                               else server.combos)
                    self._send(200, {"combos": payload})
                elif self.path == "/api/provider-nodes":
                    self._send(200, {"nodes": list(server.nodes.values())})
                elif self.path == "/api/providers":
                    self._send(200, {"connections": server.providers})
                elif self.path == "/api/settings":
                    self._send(200, dict(server.settings))
                else:
                    self._send(404, {"error": "nope"})

        self.calls = []
        self.nodes = {}
        self.providers = []
        self.combos = []
        self.readback_override = None
        self.error_path = None
        self.error_secret = "SHOULD-NOT-LEAK"
        self.settings = {"comboStrategy": "priority", "fallbackStrategy": "fill-first"}
        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever,
                         daemon=True).start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()

    def mutating_calls(self):
        return [c for c in self.calls
                if c[0] in ("POST", "PATCH") and c[1] != "/api/auth/login"]


class TestApplyPlan:
    def _policy_at(self, tmp_path, url, *, guard=True, enable_bai=False,
                   name="policy.yaml"):
        data = policy_dict()
        if enable_bai:
            for backend in data["backends"]:
                if backend["id"] == "bai-flash":
                    backend["enabled"] = True
                    backend["requires_canary"] = False
        data["gateway"] = {
            "wrapper_base_url": "http://127.0.0.1:8080",
            "node_prefix": "jevwrap",
            "targets": [{"name": "local", "url": url,
                         "kind": "disposable"}],
            "assume_core_guard": guard,
        }
        return load_policy(write_policy(tmp_path, data, name))

    def _cred(self, tmp_path, **kw):
        data = {"management_password": StubRouter.PASSWORD,
                "upstream_key": "uk"}
        data.update(kw)
        return _write_cred(tmp_path, data)

    def test_apply_happy_path_with_readback(self, tmp_path):
        router = StubRouter()
        try:
            policy = self._policy_at(tmp_path, router.url, enable_bai=True)
            out = apply_plan(policy, target_name="local",
                             credential_file=self._cred(tmp_path))
            assert out["readback"] == "verified"
            names = [c["name"] for c in router.combos]
            assert "jev.worker.code.easy" in names
            assert "jev.worker.code.standard" in names
            # Held route was never written.
            assert "jev.reviewer.review.standard" not in names
            assert out["held"] == ["jev.reviewer.review.standard"]
            # Only eligible members were registered.
            easy = next(c for c in router.combos
                        if c["name"] == "jev.worker.code.easy")
            assert easy["models"] == [
                "jevwrap/bai/deepseek-v4.1-flash",
                "jevwrap/devin/swe-2-max",
            ]
            # Login happened first and every call carried the session cookie.
            assert router.calls[0][1] == "/api/auth/login"
            assert router.providers[0]["provider"] == "node_1"
            assert router.settings["comboStrategy"] == "fallback"
        finally:
            router.close()

    def test_apply_refuses_nonoperational_combos_before_any_write(
            self, tmp_path):
        """assume_core_guard unattested -> every combo is non-operational;
        apply must fail closed with ZERO mutating HTTP calls."""
        router = StubRouter()
        try:
            policy = self._policy_at(tmp_path, router.url, guard=False)
            with pytest.raises(CompileError, match="non-operational"):
                apply_plan(policy, target_name="local",
                           credential_file=self._cred(tmp_path))
            assert router.mutating_calls() == []
            assert router.combos == [] and router.nodes == {}
        finally:
            router.close()

    def test_apply_held_only_never_writes_combos(self, tmp_path):
        router = StubRouter()
        try:
            data = policy_dict()
            data["routes"] = {
                "reviewer.review.standard": {
                    "candidates": ["devin-opus-review"]},
            }
            data["gateway"] = {
                "wrapper_base_url": "http://127.0.0.1:8080",
                "node_prefix": "jevwrap",
                "targets": [{"name": "local", "url": router.url,
                             "kind": "disposable"}],
                "assume_core_guard": True,
            }
            policy = load_policy(write_policy(tmp_path, data))
            out = apply_plan(policy, target_name="local",
                             credential_file=self._cred(tmp_path))
            assert out["held"] == ["jev.reviewer.review.standard"]
            assert router.combos == []
        finally:
            router.close()

    def test_apply_refuses_production_target(self, tmp_path, policy):
        with pytest.raises(CompileError, match="not disposable"):
            apply_plan(policy, target_name="prod",
                       credential_file=self._cred(tmp_path))

    def test_apply_refuses_non_loopback(self, tmp_path):
        data = policy_dict()
        data["gateway"] = {
            "wrapper_base_url": "http://127.0.0.1:8080",
            "node_prefix": "jevwrap",
            "targets": [{"name": "remote",
                         "url": "http://10.0.0.5:20129",
                         "kind": "disposable"}],
        }
        policy = load_policy(write_policy(tmp_path, data))
        with pytest.raises(CompileError, match="not loopback"):
            apply_plan(policy, target_name="remote",
                       credential_file=self._cred(tmp_path))

    def test_apply_refuses_lookalike_host(self, tmp_path):
        """String-split parsing used to miss suffix tricks; urlparse must not."""
        data = policy_dict()
        data["gateway"] = {
            "wrapper_base_url": "http://127.0.0.1:8080",
            "node_prefix": "jevwrap",
            "targets": [{"name": "evil",
                         "url": "http://127.0.0.1.attacker.example",
                         "kind": "disposable"}],
        }
        policy = load_policy(write_policy(tmp_path, data))
        with pytest.raises(CompileError, match="not loopback"):
            apply_plan(policy, target_name="evil",
                       credential_file=self._cred(tmp_path))

    def test_apply_refuses_installed_service_port(self, tmp_path):
        """Port 20128 is the installed service — refused even when the
        operator marks the target disposable."""
        data = policy_dict()
        data["gateway"] = {
            "wrapper_base_url": "http://127.0.0.1:8080",
            "node_prefix": "jevwrap",
            "targets": [{"name": "installed",
                         "url": "http://127.0.0.1:20128",
                         "kind": "disposable"}],
        }
        policy = load_policy(write_policy(tmp_path, data))
        with pytest.raises(CompileError, match="20128"):
            apply_plan(policy, target_name="installed",
                       credential_file=self._cred(tmp_path))

    def test_apply_credential_file_checks(self, tmp_path):
        router = StubRouter()
        try:
            policy = self._policy_at(tmp_path, router.url)
            with pytest.raises(CompileError, match="credential file"):
                apply_plan(policy, target_name="local",
                           credential_file=str(tmp_path / "missing"))
            # Group/world-readable credential files are refused.
            loose = _write_cred(tmp_path, {
                "management_password": StubRouter.PASSWORD,
                "upstream_key": "uk"}, mode=0o644, name="loose.json")
            with pytest.raises(CompileError, match="mode|permission"):
                apply_plan(policy, target_name="local",
                           credential_file=loose)
            # Symlinks are refused.
            good = _write_cred(tmp_path, {
                "management_password": StubRouter.PASSWORD,
                "upstream_key": "uk"}, name="good.json")
            link = tmp_path / "link.json"
            os.symlink(good, link)
            with pytest.raises(CompileError, match="symlink"):
                apply_plan(policy, target_name="local",
                           credential_file=str(link))
            # Missing keys.
            bad = _write_cred(tmp_path, {"management_password": "x"},
                            name="nokeys.json")
            with pytest.raises(CompileError, match="upstream_key"):
                apply_plan(policy, target_name="local",
                           credential_file=bad)
            assert router.mutating_calls() == []
        finally:
            router.close()

    def test_apply_login_failure_is_fixed_message(self, tmp_path):
        router = StubRouter()
        try:
            policy = self._policy_at(tmp_path, router.url)
            cred = _write_cred(tmp_path, {
                "management_password": "WRONG", "upstream_key": "uk"})
            with pytest.raises(CompileError, match="login"):
                apply_plan(policy, target_name="local",
                           credential_file=cred)
            assert router.mutating_calls() == []
        finally:
            router.close()

    def test_apply_management_cookie(self, tmp_path):
        router = StubRouter()
        try:
            policy = self._policy_at(tmp_path, router.url)
            cred = _write_cred(tmp_path, {
                "management_cookie": StubRouter.COOKIE,
                "upstream_key": "uk"})
            out = apply_plan(policy, target_name="local",
                             credential_file=cred)
            assert out["readback"] == "verified"
        finally:
            router.close()

    def test_apply_error_body_is_never_leaked(self, tmp_path):
        """A management error body carrying the upstream key must never reach
        the exception text — errors carry status + path only."""
        router = StubRouter()
        try:
            router.error_path = "/api/providers"
            router.error_secret = "uk-secret-body-echo"
            policy = self._policy_at(tmp_path, router.url)
            with pytest.raises(CompileError) as exc:
                apply_plan(policy, target_name="local",
                           credential_file=self._cred(tmp_path))
            assert "uk-secret-body-echo" not in str(exc.value)
            assert "HTTP 500" in str(exc.value)
        finally:
            router.close()

    def test_readback_mismatch_is_hard_failure(self, tmp_path):
        router = StubRouter()
        try:
            policy = self._policy_at(tmp_path, router.url)
            router.readback_override = [
                {"name": "jev.worker.code.easy", "models": ["wrong/order"]},
            ]
            with pytest.raises(CompileError, match="readback"):
                apply_plan(policy, target_name="local",
                           credential_file=self._cred(tmp_path))
        finally:
            router.close()

    def test_apply_without_wrapper_base_has_no_provider(self, tmp_path):
        router = StubRouter()
        try:
            data = policy_dict()
            data["routes"] = {
                "worker.code.easy": {"candidates": ["devin-swe-2-max"]},
            }
            data["gateway"] = {
                "wrapper_base_url": None,
                "node_prefix": "jevwrap",
                "targets": [{"name": "local", "url": router.url,
                             "kind": "disposable"}],
                "assume_core_guard": True,
            }
            policy = load_policy(write_policy(tmp_path, data))
            out = apply_plan(policy, target_name="local",
                             credential_file=self._cred(tmp_path))
            assert router.nodes == {} and router.providers == []
            assert router.combos[0]["models"] == ["jevwrap/devin/swe-2-max"]
            assert out["readback"] == "verified"
        finally:
            router.close()
