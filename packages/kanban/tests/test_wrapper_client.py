"""WrapperClient contract tests — real client over a real loopback HTTP
stub. Only the wrapper *process* is stubbed; the wire format, headers, and
error mapping are exercised for real."""

from __future__ import annotations

import pytest

from cli_provider_kanban.wrapper_client import (
    WrapperClient,
    WrapperHTTPError,
    WrapperTransportError,
)

from conftest import stub_wrapper  # noqa: F401  (fixture)


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
    cred = tmp_path / "cred"
    cred.write_text("tok-abc\n")
    client = WrapperClient(stub_wrapper.base_url,
                           credential_file=str(cred))
    client.submit_chat(model="m", task_id="t_k", workspace_id="w",
                       messages=[])
    assert stub_wrapper.requests[0]["headers"].get("Authorization") == \
        "Bearer tok-abc"


def test_no_credential_no_auth_header(stub_wrapper):
    client = WrapperClient(stub_wrapper.base_url)
    client.submit_chat(model="m", task_id="t_n", workspace_id="w",
                       messages=[])
    assert "Authorization" not in stub_wrapper.requests[0]["headers"]
