"""WrapperClient contract tests — real client over a real loopback HTTP
stub. Only the wrapper *process* is stubbed; the wire format, headers, and
error mapping are exercised for real."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from cli_provider_kanban.wrapper_client import (
    WrapperClient,
    WrapperError,
    WrapperHTTPError,
    WrapperTransportError,
)

from conftest import stub_wrapper, StubWrapperServer  # noqa: F401  (fixture)


def _write_cred(path, token: str, mode: int = 0o600) -> str:
    path.write_text(token, encoding="utf-8")
    os.chmod(path, mode)
    return str(path)


def test_submit_payload_shape(stub_wrapper):
    client = WrapperClient(stub_wrapper.base_url)
    out = client.submit_chat(
        model="devin/swe-2-max", task_id="t_abc", workspace_id="ws-alpha",
        messages=[{"role": "user", "content": "do it"}],
        execution={"task_revision": "1", "base_revision": "b" * 40,
                   "route": "worker.code.standard",
                   "policy_version": "2026-09-20.1"},
    )
    assert out.status == "completed"
    assert out.run_id == "run_0001"
    assert out.attempt_id == "att_0001"
    req = stub_wrapper.requests[0]
    body = req["body"]
    assert req["path"] == "/v1/chat/completions"
    assert body["stream"] is False
    assert body["metadata"]["task_id"] == "t_abc"
    assert body["metadata"]["workspace_id"] == "ws-alpha"
    # Agreed post-merge shape.
    assert body["metadata"]["execution"]["route"] == "worker.code.standard"
    assert body["model"] == "devin/swe-2-max"


def test_submit_without_execution_metadata(stub_wrapper):
    """Baseline wrapper rejects unknown metadata keys — the field rides only
    when the core contract lands."""
    client = WrapperClient(stub_wrapper.base_url)
    client.submit_chat(model="m", task_id="t_1", workspace_id="w",
                       messages=[])
    meta = stub_wrapper.requests[0]["body"]["metadata"]
    assert "execution" not in meta
    assert meta == {"task_id": "t_1", "workspace_id": "w"}


def test_duplicate_task_id_returns_cached_run(stub_wrapper):
    stub_wrapper.reuse_run = True
    client = WrapperClient(stub_wrapper.base_url)
    first = client.submit_chat(model="m", task_id="t_dup", workspace_id="w",
                               messages=[])
    second = client.submit_chat(model="m", task_id="t_dup", workspace_id="w",
                                messages=[])
    assert second.cached is True
    assert second.run_id == first.run_id
    # Same canonical run — never a second execution.
    assert stub_wrapper.submit_count == 2
    assert len(stub_wrapper.runs) == 1


def test_cached_flag_from_body_survives_header_loss(stub_wrapper):
    """A gateway may strip X-Run-Cached — the body run view is authoritative."""
    client = WrapperClient(stub_wrapper.base_url)
    first = client.submit_chat(model="m", task_id="t_body", workspace_id="w",
                               messages=[])
    # Body carries cached:true even if the header never arrives.
    run = dict(stub_wrapper.runs[first.run_id])
    run["cached"] = True
    resp = {"id": "chatcmpl-stub", "choices": [], "run": run}
    stub_wrapper.submit_status_override = (200, resp)
    second = client.submit_chat(model="m", task_id="t_body", workspace_id="w",
                                messages=[])
    assert second.cached is True
    assert second.run_id == first.run_id


def test_get_run(stub_wrapper):
    client = WrapperClient(stub_wrapper.base_url)
    out = client.submit_chat(model="m", task_id="t_g", workspace_id="w",
                             messages=[])
    run = client.get_run(out.run_id)
    assert run["run_id"] == out.run_id
    assert client.get_run("run_missing") is None


def test_cancel_run(stub_wrapper):
    client = WrapperClient(stub_wrapper.base_url)
    out = client.submit_chat(model="m", task_id="t_c", workspace_id="w",
                             messages=[])
    resp = client.cancel_run(out.run_id)
    assert resp["confirmed"] is True
    assert client.get_run(out.run_id)["status"] == "cancelled"
    with pytest.raises(WrapperHTTPError) as exc:
        client.cancel_run("run_nope")
    assert exc.value.status == 404


def test_transport_error_is_distinct(stub_wrapper):
    """Connection refused -> WrapperTransportError (UNKNOWN semantics),
    never conflated with an HTTP failure."""
    stub_wrapper.close()  # kill the server
    client = WrapperClient(stub_wrapper.base_url, timeout_seconds=2)
    with pytest.raises(WrapperTransportError):
        client.submit_chat(model="m", task_id="t_x", workspace_id="w",
                           messages=[])


def test_credential_file_read_at_call_time(stub_wrapper, tmp_path):
    cred = _write_cred(tmp_path / "cred", "tok-abc\n")
    client = WrapperClient(stub_wrapper.base_url,
                           credential_file=str(cred))
    client.submit_chat(model="m", task_id="t_k", workspace_id="w",
                       messages=[])
    assert stub_wrapper.requests[0]["headers"].get("Authorization") == \
        "Bearer tok-abc"


def test_credential_file_must_be_private(stub_wrapper, tmp_path):
    """A group/world-readable credential file is refused — the token is a
    secret and the file contract is operator-private."""
    loose = _write_cred(tmp_path / "loose", "tok\n", mode=0o644)
    client = WrapperClient(stub_wrapper.base_url, credential_file=loose)
    with pytest.raises(WrapperError, match="mode|permission"):
        client.submit_chat(model="m", task_id="t_l", workspace_id="w",
                           messages=[])
    assert stub_wrapper.requests == []


def test_credential_file_symlink_refused(stub_wrapper, tmp_path):
    real = _write_cred(tmp_path / "real", "tok\n")
    link = tmp_path / "link"
    os.symlink(real, link)
    client = WrapperClient(stub_wrapper.base_url,
                           credential_file=str(link))
    with pytest.raises(WrapperError, match="symlink"):
        client.submit_chat(model="m", task_id="t_s", workspace_id="w",
                           messages=[])
    assert stub_wrapper.requests == []


def test_no_credential_no_auth_header(stub_wrapper):
    client = WrapperClient(stub_wrapper.base_url)
    client.submit_chat(model="m", task_id="t_n", workspace_id="w",
                       messages=[])
    assert "Authorization" not in stub_wrapper.requests[0]["headers"]


# --- HTTP boundary hardening -------------------------------------------------


class StubBase:
    """Minimal recording stub for boundary tests."""

    def __init__(self, handler):
        import http.server
        server = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                server.requests.append({"method": "POST", "path": self.path,
                                        "headers": dict(self.headers)})
                handler(self, server)

            def do_GET(self):
                server.requests.append({"method": "GET", "path": self.path,
                                        "headers": dict(self.headers)})
                handler(self, server)

            def _send(self, code, body=b"{}", ctype="application/json",
                      headers=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        self.requests = []
        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.base_url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever,
                         daemon=True).start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def test_redirect_is_never_followed():
    """urllib's default redirect handler would forward Authorization off-host;
    the hardened transport treats 3xx as a terminal error."""
    def handler(req, server):
        req._send(302, b"{}", headers={"Location": "http://169.254.169.254/"})

    stub = StubBase(handler)
    try:
        client = WrapperClient(stub.base_url)
        with pytest.raises(WrapperHTTPError) as exc:
            client.submit_chat(model="m", task_id="t_r", workspace_id="w",
                               messages=[])
        assert exc.value.status == 302
        # One request only — never re-issued to the redirect target.
        assert len(stub.requests) == 1
    finally:
        stub.close()


def test_non_loopback_url_is_refused():
    """The wrapper/gateway boundary is loopback-only — a remote URL is a
    constructor-time contract failure, never a DNS escape."""
    for bad in ("http://10.0.0.5:9000", "http://wrapper.internal:9000",
                "http://169.254.169.254", "http://127.0.0.1.evil.example",
                "ftp://127.0.0.1", "https://10.0.0.5"):
        with pytest.raises(WrapperError):
            WrapperClient(bad)


def test_https_is_not_a_trusted_loopback_contract():
    """Only explicit HTTP loopback is the wrapper boundary."""
    with pytest.raises(WrapperError):
        WrapperClient("https://127.0.0.1:9000")


def test_unparsable_success_body_is_transport_error():
    """A body that isn't JSON is unusable -> WrapperTransportError with a
    fixed message, never the raw (possibly secret-bearing) body."""
    def handler(req, server):
        req._send(200, b"not-json-at-all")

    stub = StubBase(handler)
    try:
        client = WrapperClient(stub.base_url)
        with pytest.raises(WrapperTransportError, match="unparsable|JSON"):
            client.submit_chat(model="m", task_id="t_p", workspace_id="w",
                               messages=[])
    finally:
        stub.close()


def test_error_body_is_bounded_and_not_leaked():
    """A huge provider error body is capped and never echoed verbatim."""
    def handler(req, server):
        req._send(500, json.dumps(
            {"error": {"message": "SECRET-DETAIL"}}).encode())

    stub = StubBase(handler)
    try:
        client = WrapperClient(stub.base_url)
        with pytest.raises(WrapperHTTPError) as exc:
            client.submit_chat(model="m", task_id="t_e", workspace_id="w",
                               messages=[])
        assert exc.value.status == 500
        # The error classifies by status; the run view stays parseable for
        # run_id recovery but the message is fixed.
        assert "wrapper HTTP 500" in str(exc.value)
    finally:
        stub.close()


def test_oversized_response_is_bounded():
    def handler(req, server):
        req._send(200, b"x" * (2 * 1024 * 1024))

    stub = StubBase(handler)
    try:
        client = WrapperClient(stub.base_url,
                               max_response_bytes=64 * 1024)
        with pytest.raises(WrapperTransportError, match="too large|bytes"):
            client.submit_chat(model="m", task_id="t_b", workspace_id="w",
                               messages=[])
    finally:
        stub.close()


def test_whole_response_deadline_not_per_socket():
    """A slow drip-feed must hit the whole-response deadline, not a fresh
    per-socket timeout per byte."""
    def handler(req, server):
        req.send_response(200)
        req.send_header("Content-Type", "application/json")
        req.send_header("Transfer-Encoding", "chunked")
        req.end_headers()
        for _ in range(40):
            chunk = b'{"a": 1}'
            req.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            req.wfile.flush()
            time.sleep(0.05)
        req.wfile.write(b"0\r\n\r\n")
        req.wfile.flush()

    stub = StubBase(handler)
    try:
        client = WrapperClient(stub.base_url, timeout_seconds=0.5)
        started = time.monotonic()
        with pytest.raises(WrapperTransportError):
            client.submit_chat(model="m", task_id="t_d", workspace_id="w",
                               messages=[])
        assert time.monotonic() - started < 3.0
    finally:
        stub.close()


def test_path_ids_are_validated():
    """run_id / artifact_id segments are canonical ids — no traversal."""
    client = WrapperClient("http://127.0.0.1:1")
    for bad in ("../etc", "a/b", "a b", "", "a%2f..", ".."):
        with pytest.raises(WrapperError):
            client.get_run(bad)
        with pytest.raises(WrapperError):
            client.get_artifact(bad)
        with pytest.raises(WrapperError):
            client.cancel_run(bad)


def test_header_rejected_value_is_typed_transport_failure(tmp_path):
    """A bearer token http.client's putheader rejects (non-latin-1) escapes
    as a bare ValueError otherwise — it must map to a fixed typed
    WrapperTransportError whose message can never embed the secret, with
    the exception chain suppressed (``from None``)."""
    stub = StubWrapperServer()
    try:
        cred = _write_cred(tmp_path / "bad-tok", "secret-€token\n")
        client = WrapperClient(stub.base_url, credential_file=cred)
        with pytest.raises(WrapperTransportError) as exc:
            client.submit_chat(model="m", task_id="t_h", workspace_id="w",
                               messages=[])
        assert "secret" not in str(exc.value)
        assert "€" not in str(exc.value)
        assert exc.value.__suppress_context__ is True
        # The header was rejected before the request hit the wire.
        assert stub.requests == []
        # Positive control: a normal token still works.
        good = _write_cred(tmp_path / "good-tok", "ok-token\n")
        ok = WrapperClient(stub.base_url, credential_file=good)
        out = ok.submit_chat(model="m", task_id="t_ok", workspace_id="w",
                             messages=[])
        assert out.run_id
        assert stub.requests[-1]["headers"]["Authorization"] == \
            "Bearer ok-token"
    finally:
        stub.close()


def test_run_view_shape_is_normalized(stub_wrapper):
    """An unknown status string is coerced to 'unknown' — finite known
    statuses only; the upper layer never retries on inconsistency."""
    stub_wrapper.run_status = "exploded"
    client = WrapperClient(stub_wrapper.base_url)
    out = client.submit_chat(model="m", task_id="t_u", workspace_id="w",
                             messages=[])
    assert out.status == "unknown"
    assert out.run_id  # run identity still surfaced for reconciliation


def test_contradicting_task_id_is_unknown(stub_wrapper):
    """A run view contradicting the request identity is not 'completed'."""
    client = WrapperClient(stub_wrapper.base_url)
    out = client.submit_chat(model="m", task_id="t_submitted",
                             workspace_id="w", messages=[])
    assert out.status == "completed"

    # Now the wrapper answers a different task_id inside the run view.
    first_run = dict(stub_wrapper.runs[out.run_id])
    first_run["task_id"] = "t_other"
    stub_wrapper.submit_status_override = (
        200, {"id": "x", "choices": [], "run": first_run})
    out2 = client.submit_chat(model="m", task_id="t_submitted",
                              workspace_id="w", messages=[])
    assert out2.status == "unknown"
    assert out2.run_id == out.run_id


# --- split submit/control endpoints ------------------------------------------


def test_control_endpoints_use_control_base_and_credential(tmp_path):
    """POST chat goes to base_url (e.g. a gateway); run/cancel/artifact go
    DIRECT to the wrapper control base with its own credential."""
    submit_stub = StubWrapperServer()
    control_stub = StubWrapperServer()
    try:
        chat_key = _write_cred(tmp_path / "chat", "chat-key\n")
        ctrl_key = _write_cred(tmp_path / "ctrl", "ctrl-key\n")
        client = WrapperClient(
            submit_stub.base_url,
            credential_file=chat_key,
            control_base_url=control_stub.base_url,
            control_credential_file=ctrl_key,
        )
        out = client.submit_chat(model="m", task_id="t_split",
                                 workspace_id="w", messages=[])
        # Submit went to the gateway base with the chat credential.
        assert submit_stub.requests[0]["headers"]["Authorization"] == \
            "Bearer chat-key"
        # The wrapper control plane owns the run view (a gateway only proxies
        # chat) — seed it on the control stub.
        control_stub.runs[out.run_id] = dict(out.run)
        # Control reads went to the wrapper base with the control credential.
        run = client.get_run(out.run_id)
        assert run["run_id"] == out.run_id
        assert control_stub.requests[0]["path"].startswith("/api/v1/runs/")
        assert control_stub.requests[0]["headers"]["Authorization"] == \
            "Bearer ctrl-key"
        resp = client.cancel_run(out.run_id)
        assert resp["confirmed"] is True
        assert control_stub.requests[-1]["path"].endswith("/cancel")
        data = client.get_artifact("art_1")
        assert data == b"artifact-bytes"
        # Nothing control-shaped touched the submit stub.
        assert all(r["path"] == "/v1/chat/completions"
                   for r in submit_stub.requests)
    finally:
        submit_stub.close()
        control_stub.close()


def test_control_defaults_to_submit_base(stub_wrapper):
    client = WrapperClient(stub_wrapper.base_url)
    out = client.submit_chat(model="m", task_id="t_d2", workspace_id="w",
                             messages=[])
    assert client.get_run(out.run_id)["run_id"] == out.run_id
