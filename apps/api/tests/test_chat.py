"""Chat completions: sync, SSE, unsupported-before-execution, limits, scoping."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from conftest import MockSystem
from test_lifecycle import _StreamThread

CHAT = "/v1/chat/completions"


def body(model="mock/text", content="hello", task="task-1", workspace="ws-alpha", **extra):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "metadata": {"task_id": task, "workspace_id": workspace},
    }
    payload.update(extra)
    return payload


def sse_payloads(text: str) -> list:
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            raw = line[len("data: ") :]
            if raw == "[DONE]":
                out.append("[DONE]")
            else:
                out.append(json.loads(raw))
    return out


def test_nonstream_success_returns_chat_completion_with_run_extension(system: MockSystem):
    with system.client() as client:
        response = client.post(CHAT, json=body())
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "mock/text"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert "hello" in data["choices"][0]["message"]["content"]
    assert data["usage"] is None  # unknown usage is null, never zero

    run = data["run"]
    assert run["run_id"] and run["task_id"] == "task-1" and run["attempt_id"]
    assert run["preset"] == "mock/text" and run["runner_instance"] == "runner-1"
    assert run["status"] == "completed" and run["outcome"] == "succeeded"
    assert run["verification"]["status"] == "not_run"
    assert run["usage"]["provenance"] == "unknown"
    assert run["artifacts"]
    assert response.headers["X-Run-Id"] == run["run_id"]


def test_messages_preserve_all_roles(system: MockSystem):
    payload = {
        "model": "mock/text",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "again"},
        ],
        "metadata": {"task_id": "task-roles", "workspace_id": "ws-alpha"},
    }
    with system.client() as client:
        response = client.post(CHAT, json=payload)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"]


def test_unknown_model_is_not_found(system: MockSystem):
    with system.client() as client:
        response = client.post(CHAT, json=body(model="nope/text"))
    assert response.status_code == 404


def test_preset_outside_principal_scope_is_forbidden(system: MockSystem):
    with system.client() as client:  # alpha may only use mock/text
        response = client.post(CHAT, json=body(model="mock/review"))
    assert response.status_code == 403


def test_workspace_outside_principal_scope_is_forbidden(system: MockSystem):
    with system.client() as client:
        response = client.post(CHAT, json=body(workspace="ws-beta"))
    assert response.status_code == 403


def test_provider_scoped_chat_rejects_other_driver(system: MockSystem):
    with system.client() as client:
        ok = client.post("/providers/mock/v1/chat/completions", json=body())
        assert ok.status_code == 200
        other = client.post(
            "/providers/other/v1/chat/completions",
            json=body(content="other", task="task-2"),
        )
    assert other.status_code == 404


@pytest.mark.parametrize(
    "extra",
    [
        {"tools": [{"type": "function"}]},
        {"tool_choice": "auto"},
        {"temperature": 0.5},
        {"max_tokens": 10},
        {"response_format": {"type": "json_object"}},
        {"n": 2},
        {"seed": 1},
    ],
)
def test_unsupported_fields_are_rejected_before_execution(system: MockSystem, extra):
    with system.client() as client:
        response = client.post(CHAT, json=body(**extra))
        assert response.status_code == 422
        assert response.json()["error"]["type"] == "unsupported_capability"
        assert "X-Run-Id" not in response.headers
        # The task_id was never reserved: a valid request now succeeds fresh.
        followup = client.post(CHAT, json=body(content="clean"))
    assert followup.status_code == 200


def test_unknown_extra_field_is_invalid_request(system: MockSystem):
    with system.client() as client:
        response = client.post(CHAT, json=body(executable="/bin/sh"))
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_message_level_execution_fields_are_rejected_before_execution(system: MockSystem):
    # Execution-selecting keys on a message are unsupported, never silently
    # dropped, and the task id is never reserved.
    with system.client() as client:
        for message in (
            {"role": "user", "content": "hi", "function_call": {"name": "x"}},
            {"role": "assistant", "content": "hi", "tool_calls": [], "name": "spoof"},
        ):
            payload = body(task="task-msg")
            payload["messages"] = [message]
            response = client.post(CHAT, json=payload)
            assert response.status_code == 422
            assert response.json()["error"]["type"] == "unsupported_capability"
            assert "X-Run-Id" not in response.headers
        followup = client.post(CHAT, json=body(task="task-msg", content="clean"))
    assert followup.status_code == 200


def test_unknown_message_field_is_invalid_request(system: MockSystem):
    payload = body(task="task-msg-unknown")
    payload["messages"] = [{"role": "user", "content": "hi", "extra_key": 1}]
    with system.client() as client:
        response = client.post(CHAT, json=payload)
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request_error"
        assert "X-Run-Id" not in response.headers
        followup = client.post(CHAT, json=body(task="task-msg-unknown", content="clean"))
    assert followup.status_code == 200



def test_image_content_is_rejected(system: MockSystem):
    payload = body()
    payload["messages"] = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {}}],
        }
    ]
    with system.client() as client:
        response = client.post(CHAT, json=payload)
    assert response.status_code == 422


def test_metadata_requires_task_and_workspace(system: MockSystem):
    with system.client() as client:
        missing_task = client.post(
            CHAT,
            json={"model": "mock/text", "messages": [{"role": "user", "content": "x"}],
                  "metadata": {"workspace_id": "ws-alpha"}},
        )
        missing_workspace = client.post(
            CHAT,
            json={"model": "mock/text", "messages": [{"role": "user", "content": "x"}],
                  "metadata": {"task_id": "task-1"}},
        )
    assert missing_task.status_code == 400
    assert missing_workspace.status_code == 400


def test_chunked_body_over_limit_is_rejected(system: MockSystem):
    big = json.dumps(body(content="x" * 20000)).encode()
    with system.client() as client:
        response = client.post(
            CHAT, content=iter([big]), headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 413


def test_slow_request_body_is_bounded_by_a_fixed_deadline(system_factory):
    # A byte cap alone does not bound a drip feed: the whole body read runs
    # under one fixed deadline that is never renewed per chunk.
    import socket as _socket

    system = system_factory(
        "success", config_overrides={"api": {"request_body_timeout_seconds": 0.5}}
    )
    payload = json.dumps(body()).encode()
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Authorization: Bearer local-alpha-key\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
    )
    sock = _socket.create_connection(("127.0.0.1", system.port), timeout=10)
    try:
        sock.sendall(request)
        sock.sendall(payload[: len(payload) // 2])  # then stall
        sock.settimeout(10)
        received = sock.recv(8192).decode("utf-8", "replace")
    finally:
        sock.close()
    assert "408" in received.split("\r\n")[0]


def test_sse_streams_only_message_deltas_then_done(system: MockSystem):
    with system.client() as client:
        with client.stream("POST", CHAT, json=body(stream=True)) as response:
            assert response.status_code == 200
            assert response.headers["X-Run-Id"]
            text = "".join(response.iter_text())
    payloads = sse_payloads(text)
    assert payloads[-1] == "[DONE]"
    chunks = [p for p in payloads if p != "[DONE]"]
    assert chunks[0]["object"] == "chat.completion.chunk"
    content = "".join(
        p["choices"][0]["delta"].get("content", "")
        for p in chunks
        if "choices" in p
    )
    assert "hello" in content
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    # Structural events are never streamed as answer content.
    assert "run.started" not in text
    assert '"tool.' not in text
    assert '"kind"' not in text


def test_sse_first_and_final_chunks_carry_run_identity_and_outcome(system: MockSystem):
    with system.client() as client:
        with client.stream("POST", CHAT, json=body(stream=True)) as response:
            run_id = response.headers["X-Run-Id"]
            text = "".join(response.iter_text())
    chunks = [p for p in sse_payloads(text) if p != "[DONE]"]
    # First chunk: run identity in metadata, never in the answer text.
    first = chunks[0]
    assert first["run"]["run_id"] == run_id
    assert first["run"]["status"] in {"queued", "starting", "running"}
    assert first["choices"][0]["delta"].get("content") is None
    # Final chunk: normalized outcome/verification/artifact/cached metadata.
    last = chunks[-1]
    assert last["choices"][0]["finish_reason"] == "stop"
    assert last["run"]["status"] == "completed"
    assert last["run"]["outcome"] == "succeeded"
    assert last["run"]["verification"]["status"] == "not_run"
    assert last["run"]["artifacts"]
    assert last["run"]["cached"] is False


def test_cached_sse_stream_carries_run_identity(system: MockSystem):
    with system.client() as client:
        first = client.post(CHAT, json=body(task="task-cached-sse"))
        assert first.status_code == 200
        run_id = first.headers["X-Run-Id"]
        with client.stream(
            "POST", CHAT, json=body(task="task-cached-sse", stream=True)
        ) as response:
            assert response.headers["X-Run-Cached"] == "true"
            text = "".join(response.iter_text())
    chunks = [p for p in sse_payloads(text) if p != "[DONE]"]
    assert chunks[0]["run"]["run_id"] == run_id
    assert chunks[0]["run"]["cached"] is True
    assert chunks[-1]["run"]["cached"] is True
    assert chunks[-1]["run"]["status"] == "completed"


def test_completion_id_is_deterministically_bound_to_the_run(system: MockSystem):
    with system.client() as client:
        response = client.post(CHAT, json=body(task="task-idbind"))
        assert response.status_code == 200
        data = response.json()
        run_id = data["run"]["run_id"]
        assert run_id.startswith("run_")
        assert data["id"] == f"chatcmpl-{run_id}"
        # A cached replay reuses the exact same bound id, not a fresh random one.
        cached = client.post(CHAT, json=body(task="task-idbind"))
        assert cached.status_code == 200
        assert cached.headers["X-Run-Cached"] == "true"
        assert cached.json()["id"] == data["id"]
        assert cached.headers["X-Run-Id"] == run_id


def test_sse_completion_ids_are_bound_to_the_run_live_and_cached(system: MockSystem):
    with system.client() as client:
        with client.stream(
            "POST", CHAT, json=body(task="task-idbind-sse", stream=True)
        ) as response:
            header_run = response.headers["X-Run-Id"]
            text = "".join(response.iter_text())
        expected = f"chatcmpl-{header_run}"
        chunks = [p for p in sse_payloads(text) if p != "[DONE]"]
        assert chunks and all(c["id"] == expected for c in chunks)
        assert chunks[0]["run"]["run_id"] == header_run
        assert chunks[-1]["run"]["run_id"] == header_run

        # Cached SSE keeps the same bound id (identity without the initial chunk).
        with client.stream(
            "POST", CHAT, json=body(task="task-idbind-sse", stream=True)
        ) as response:
            assert response.headers["X-Run-Cached"] == "true"
            cached_text = "".join(response.iter_text())
    cached_chunks = [p for p in sse_payloads(cached_text) if p != "[DONE]"]
    assert cached_chunks and all(c["id"] == expected for c in cached_chunks)


def test_metadata_task_policy_override_is_rejected_before_execution(system: MockSystem):
    payload = body()
    payload["metadata"]["task_policy"] = "review"
    with system.client() as client:
        response = client.post(CHAT, json=payload)
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "invalid_request_error"
        assert "X-Run-Id" not in response.headers
        # The task id was never reserved: a clean request now succeeds fresh.
        followup = client.post(CHAT, json=body())
    assert followup.status_code == 200


def test_many_events_nonstream_and_late_sse_complete(system_factory):
    system = system_factory(
        "many_events",
        config_overrides={
            "api": {
                "default_run_deadline_seconds": 30.0,
                "max_run_deadline_seconds": 60.0,
            }
        },
    )
    with system.client() as client:
        response = client.post(CHAT, json=body(task="many-1"))
        assert response.status_code == 200
        run = response.json()["run"]
        assert run["status"] == "completed"
        assert run["outcome"] == "succeeded"
        events = client.get(
            f"/api/v1/runs/{run['run_id']}/events?limit=10000"
        ).json()["events"]
        assert len(events) == 302
        # A late SSE subscriber must still receive the full ordered stream.
        with client.stream(
            "POST", CHAT, json=body(task="many-2", stream=True)
        ) as streamed:
            streamed_id = streamed.headers["X-Run-Id"]
            text = "".join(streamed.iter_text())
    chunks = [p for p in sse_payloads(text) if p != "[DONE]"]
    content = "".join(
        p["choices"][0]["delta"].get("content", "")
        for p in chunks
        if "choices" in p
    )
    assert content.count("[synthetic] e") == 300
    assert chunks[0]["run"]["run_id"] == streamed_id
    assert chunks[-1]["run"]["status"] == "completed"


def test_sse_structural_events_exist_but_are_not_streamed(system: MockSystem):
    with system.client() as client:
        with client.stream("POST", CHAT, json=body(stream=True)) as response:
            run_id = response.headers["X-Run-Id"]
            "".join(response.iter_text())
        events = client.get(f"/api/v1/runs/{run_id}/events").json()["events"]
    kinds = [e["kind"] for e in events]
    assert "run.started" in kinds and "run.completed" in kinds


def test_failed_run_maps_to_safe_error(run_failure_system: MockSystem):
    with run_failure_system.client() as client:
        response = client.post(CHAT, json=body())
    assert response.status_code == 502
    payload = response.json()
    assert payload["error"]["code"] == "provider_failed"
    assert payload["run"]["status"] == "failed"
    assert "prompt" not in json.dumps(payload).lower()


def test_unknown_run_maps_to_safe_error(unknown_system: MockSystem):
    with unknown_system.client() as client:
        response = client.post(CHAT, json=body())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "run_unknown"
    assert response.json()["run"]["status"] == "unknown"


def test_queue_timeout_outcome_maps_to_429(system_factory):
    # One Runner slot held by a real hanging run; a second admitted request runs
    # out of queue_timeout and must be a 429 rate-limit, not a 502 provider error.
    system = system_factory(
        "hang",
        config_overrides={
            "api": {
                "default_run_deadline_seconds": 30.0,
                "max_run_deadline_seconds": 60.0,
                "cancel_deadline_seconds": 2.0,
                "concurrency": {
                    "per_runner": 1,
                    "per_principal": 2,
                    "queue_timeout_seconds": 0.3,
                },
            }
        },
    )
    first = _StreamThread(system, body(task="qt-1")).start()
    try:
        with system.client() as client:
            deadline = time.time() + 10
            status = None
            while time.time() < deadline:
                status = client.get(f"/api/v1/runs/{first.run_id}").json()["status"]
                if status in {"starting", "running"}:
                    break
                time.sleep(0.05)
            assert status in {"starting", "running"}

            second = client.post(CHAT, json=body(task="qt-2", content="queued"))
            assert second.status_code == 429, second.text
            payload = second.json()
            assert payload["error"]["code"] == "queue_timeout"
            assert payload["error"]["type"] == "rate_limit_error"
            assert payload["run"]["status"] == "failed"
            assert payload["run"]["outcome"] == "queue_timeout"

            client.post(f"/api/v1/runs/{first.run_id}/cancel")
    finally:
        first.join()
    assert first.error is None
