"""Compiler tests — pure plan rendering plus apply against a stub 9Router
management API on loopback (the only target kind this slice permits)."""

from __future__ import annotations

import json
import threading

import pytest

from cli_provider_kanban.compiler import (
    CompileError,
    apply_plan,
    compile_plan,
)
from cli_provider_kanban.policy import load_policy

from conftest import policy_dict, write_policy


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
    def test_combos_carry_policy_order(self, policy):
        plan = compile_plan(policy)
        combos = {c["name"]: c for c in plan["combos"]}
        # easy: BAI -> Devin per the policy; standard: Devin -> BAI.
        assert combos["jev.worker.code.easy"]["models"] == [
            "jevwrap/hermes-api/bai-deepseek-v4.1-flash",
            "jevwrap/devin/swe-2-max",
        ]
        assert combos["jev.worker.code.standard"]["models"] == [
            "jevwrap/devin/swe-2-max",
            "jevwrap/hermes-api/bai-deepseek-v4.1-flash",
        ]

    def test_disabled_canary_marks_combo_nonoperational(self, policy):
        plan = compile_plan(policy)
        combos = {c["name"]: c for c in plan["combos"]}
        # bai-flash is disabled+canary -> every combo containing it is
        # non-operational.
        assert combos["jev.worker.code.easy"]["operational"] is False
        assert combos["jev.worker.code.standard"]["operational"] is False
        assert combos["jev.reviewer.review.standard"]["operational"] is False

    def test_mutation_tier_needs_core_guard(self, tmp_path):
        data = policy_dict()
        data["routes"] = {
            "worker.code.standard": {"candidates": ["devin-swe-2-max"]},
        }
        data["gateway"] = {
            "wrapper_base_url": "http://127.0.0.1:8080",
            "node_prefix": "jevwrap",
            "targets": [],
            "assume_core_guard": False,
        }
        policy = load_policy(write_policy(tmp_path, data))
        plan = compile_plan(policy)
        # devin-swe-2-max is enabled but 'standard' is a mutation tier and
        # the core guard is unattested -> non-operational.
        assert plan["combos"][0]["operational"] is False

        data["gateway"]["assume_core_guard"] = True
        policy2 = load_policy(write_policy(tmp_path, data, "p2.yaml"))
        plan2 = compile_plan(policy2)
        assert plan2["combos"][0]["operational"] is True

    def test_plan_is_secret_free(self, policy, tmp_path):
        cred = tmp_path / "cred.json"
        cred.write_text(json.dumps({
            "management_token": "SECRET-MGMT", "upstream_key": "SECRET-UP"}))
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


class StubRouter:
    """Stub 9Router management API on loopback."""

    def __init__(self):
        import http.server
        server = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def _send(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

            def do_PATCH(self):
                server.calls.append(("PATCH", self.path, self._body()))
                self._send(200, {"ok": True})

            def do_POST(self):
                body = self._body()
                server.calls.append(("POST", self.path, body))
                if self.path == "/api/provider-nodes":
                    nid = f"node_{len(server.nodes)+1}"
                    server.nodes[body["name"]] = {"id": nid, **body}
                    self._send(200, {"node": server.nodes[body["name"]]})
                elif self.path == "/api/providers":
                    server.providers.append(body)
                    self._send(200, {"ok": True})
                elif self.path == "/api/combos":
                    server.combos.append({"name": body["name"],
                                          "models": body["models"]})
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": "nope"})

            def do_GET(self):
                server.calls.append(("GET", self.path, None))
                if self.path == "/api/combos":
                    payload = (server.readback_override
                               if server.readback_override is not None
                               else server.combos)
                    self._send(200, {"combos": payload})
                else:
                    self._send(404, {"error": "nope"})

        self.calls = []
        self.nodes = {}
        self.providers = []
        self.combos = []
        self.readback_override = None
        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever,
                         daemon=True).start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


class TestApplyPlan:
    def _policy_at(self, tmp_path, url):
        data = policy_dict()
        data["gateway"] = {
            "wrapper_base_url": "http://127.0.0.1:8080",
            "node_prefix": "jevwrap",
            "targets": [{"name": "local", "url": url,
                         "kind": "disposable"}],
        }
        return load_policy(write_policy(tmp_path, data))

    def _cred(self, tmp_path):
        cred = tmp_path / "cred.json"
        cred.write_text(json.dumps({
            "management_token": "mt", "upstream_key": "uk"}))
        return str(cred)

    def test_apply_happy_path_with_readback(self, tmp_path):
        router = StubRouter()
        try:
            policy = self._policy_at(tmp_path, router.url)
            out = apply_plan(policy, target_name="local",
                             credential_file=self._cred(tmp_path))
            assert out["readback"] == "verified"
            assert router.combos  # combos were actually created
            names = [c["name"] for c in router.combos]
            assert "jev.worker.code.standard" in names
            # Provider apiKey came from the credential file, not argv.
            assert router.providers[0]["apiKey"] == "uk"
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
                         "url": "http://10.0.0.5:20128",
                         "kind": "disposable"}],
        }
        policy = load_policy(write_policy(tmp_path, data))
        with pytest.raises(CompileError, match="not loopback"):
            apply_plan(policy, target_name="remote",
                       credential_file=self._cred(tmp_path))

    def test_apply_requires_credential_file(self, tmp_path):
        router = StubRouter()
        try:
            policy = self._policy_at(tmp_path, router.url)
            with pytest.raises(CompileError, match="credential file"):
                apply_plan(policy, target_name="local",
                           credential_file=str(tmp_path / "missing"))
        finally:
            router.close()

    def test_readback_mismatch_is_hard_failure(self, tmp_path):
        router = StubRouter()
        try:
            policy = self._policy_at(tmp_path, router.url)
            # Gateway reports a drifted order on readback.
            router.readback_override = [
                {"name": "jev.worker.code.easy", "models": ["wrong/order"]},
            ]
            with pytest.raises(CompileError, match="readback"):
                apply_plan(policy, target_name="local",
                           credential_file=self._cred(tmp_path))
        finally:
            router.close()
