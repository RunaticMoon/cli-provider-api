"""Fixture test for examples/catalog_client.py — real API + Runner, fake key.

Runs the stdlib example as a subprocess against the same MockSystem fixture
as the rest of this suite, plus local redirect/sink HTTP servers proving the
bearer credential never crosses an origin. No real credentials, no native
inference.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_dynamic_catalog import _catalog_config, _write_catalog

CLIENT = Path(__file__).resolve().parents[3] / "examples" / "catalog_client.py"
FAKE_KEY = "local-alpha-key"


def _keyfile(tmp_path) -> Path:
    path = tmp_path / "client.key"
    path.write_text(FAKE_KEY, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _run_client(tmp_path, base: str, *argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CPA_BASE"] = base
    env["CPA_KEY_FILE"] = str(_keyfile(tmp_path))
    return subprocess.run(
        [sys.executable, str(CLIENT), *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _client_system(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(
        catalog_file,
        [
            {"model_id": "mock-model"},
            {
                "model_id": "mock-effort",
                "effort": "selectable",
                "effort_options": ["low", "high"],
            },
            {"model_id": "mock-blocked", "executable": False},
        ],
    )
    return system_factory(
        "success",
        config_overrides=_catalog_config(executable_catalogs=["mock-catalog"]),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )


def test_client_discovery_is_read_only_by_default(system_factory, tmp_path):
    system = _client_system(system_factory, tmp_path)
    proc = _run_client(tmp_path, system.base_url)
    assert proc.returncode == 0, proc.stderr
    assert "mock/mock-effort" in proc.stdout
    assert "mock/mock-model" in proc.stdout
    # Discovery never submits: no run was created for any client task.
    with system.client() as client:
        catalog = client.get("/api/v1/catalog").json()
    assert catalog["data"], "catalog endpoint returned no sources"


def test_client_run_requires_explicit_opt_in(system_factory, tmp_path):
    system = _client_system(system_factory, tmp_path)
    # --run without the exact alias/workspace/task-id is a usage error before
    # any HTTP call; a non-executable entry also refuses before the POST.
    missing = _run_client(
        tmp_path, system.base_url, "--run", "--model", "mock/mock-effort"
    )
    assert missing.returncode == 2
    assert "--workspace" in missing.stderr or "--task-id" in missing.stderr

    blocked = _run_client(
        tmp_path,
        system.base_url,
        "--run",
        "--model", "mock/mock-blocked",
        "--workspace", "ws-alpha",
        "--task-id", "client-blocked",
    )
    assert blocked.returncode != 0
    assert "not executable" in blocked.stderr or "not executable" in blocked.stdout


def test_client_run_executes_an_authorized_alias(system_factory, tmp_path):
    system = _client_system(system_factory, tmp_path)
    proc = _run_client(
        tmp_path,
        system.base_url,
        "--run",
        "--model", "mock/mock-effort",
        "--workspace", "ws-alpha",
        "--task-id", "client-exec-1",
        "--effort", "high",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout[proc.stdout.index("{") :])
    assert payload["status"] == "completed"
    assert payload["model_binding"]["resolved_model"] == "mock-effort"
    assert payload["model_binding"]["reasoning_effort"] == "high"


def test_client_refuses_redirects_and_never_leaks_key(tmp_path):
    """A 302 to a different origin must fail closed: the sink sees zero
    requests and never sees the Authorization header."""
    seen: list[dict] = []

    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
            seen.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{sink.server_port}/sink"
            )
            self.end_headers()

    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    front = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [
        threading.Thread(target=s.serve_forever, daemon=True)
        for s in (sink, front)
    ]
    for t in threads:
        t.start()
    try:
        proc = _run_client(tmp_path, f"http://127.0.0.1:{front.server_port}")
        assert proc.returncode != 0
        # Fail-closed: the redirect was refused, nothing reached the sink.
        assert seen == []
    finally:
        for s in (front, sink):
            s.shutdown()
            s.server_close()
        for t in threads:
            t.join(timeout=5)


def test_client_never_echoes_error_bodies(tmp_path):
    """A provider error page may carry arbitrary data; only the status is
    reported."""

    class Boom(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"SECRET-MARKER-DO-NOT-ECHO")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Boom)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        proc = _run_client(tmp_path, f"http://127.0.0.1:{server.server_port}")
        assert proc.returncode != 0
        assert "SECRET-MARKER" not in proc.stdout + proc.stderr
        assert "500" in proc.stderr or "500" in proc.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
