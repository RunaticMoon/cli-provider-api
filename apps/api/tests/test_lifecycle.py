"""Lifecycle: idempotency, conflicts, cancellation, deadline, quarantine, restart."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from conftest import MockSystem

CHAT = "/v1/chat/completions"


def body(model="mock/text", content="hello", task="task-1", workspace="ws-alpha", **extra):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "metadata": {"task_id": task, "workspace_id": workspace},
    }
    payload.update(extra)
    return payload


def _post(client: httpx.Client, payload: dict) -> httpx.Response:
    return client.post(CHAT, json=payload)


def test_identical_completed_call_replays_cached(system: MockSystem):
    with system.client() as client:
        first = _post(client, body())
        second = _post(client, body())
    assert first.status_code == 200 and second.status_code == 200
    assert second.headers.get("X-Run-Cached") == "true"
    assert second.headers["X-Run-Id"] == first.headers["X-Run-Id"]
    assert second.json()["run"]["cached"] is True


def test_changed_body_under_same_task_conflicts(system: MockSystem):
    with system.client() as client:
        assert _post(client, body()).status_code == 200
        conflict = _post(client, body(content="different"))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "task_content_conflict"


def test_different_model_on_completed_task_conflicts(system: MockSystem):
    # mock/text-beta shares mock/text's configured task policy, so this is a
    # model relabel conflict, not a content conflict.
    with system.client(key=system.gamma_key) as client:
        assert (
            _post(client, body(model="mock/text", task="task-model")).status_code == 200
        )
        conflict = _post(client, body(model="mock/text-beta", task="task-model"))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "model_conflict"


def test_different_configured_task_policy_is_a_content_conflict(system: MockSystem):
    # mock/review carries a different operator task policy, so the derived
    # request hash differs and the same task id is a content conflict.
    with system.client(key=system.gamma_key) as client:
        assert (
            _post(client, body(model="mock/text", task="task-policy")).status_code == 200
        )
        conflict = _post(client, body(model="mock/review", task="task-policy"))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "task_content_conflict"


def test_concurrent_identical_calls_execute_once(system: MockSystem):
    payload = body(task="task-dup", content="duplicate")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_post, system.client(), payload),
            pool.submit(_post, system.client(), payload),
        ]
        responses = [f.result() for f in futures]
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409]
    ok = next(r for r in responses if r.status_code == 200)
    conflict = next(r for r in responses if r.status_code == 409)
    assert conflict.json()["error"]["code"] == "run_active"
    # Same run id proves a single execution.
    assert conflict.json()["error"]["run_id"] == ok.headers["X-Run-Id"]


class _StreamThread:
    """Opens an SSE request in a thread and records its run id promptly."""

    def __init__(self, system: MockSystem, payload: dict, key: str | None = None) -> None:
        self.system = system
        self.payload = {**payload, "stream": True}
        self.key = key
        self.run_id: str | None = None
        self.status_code: int | None = None
        self.body = ""
        self.error: Exception | None = None
        self.headers_ready = threading.Event()
        self.finished = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "_StreamThread":
        self._thread.start()
        assert self.headers_ready.wait(timeout=15)
        return self

    def _run(self) -> None:
        try:
            with self.system.client(self.key) as client:
                with client.stream("POST", CHAT, json=self.payload) as response:
                    self.status_code = response.status_code
                    self.run_id = response.headers.get("X-Run-Id")
                    self.headers_ready.set()
                    self.body = "".join(response.iter_text())
        except Exception as exc:  # noqa: BLE001
            self.error = exc
            self.headers_ready.set()
        finally:
            self.finished.set()

    def join(self, timeout: float = 15.0) -> None:
        assert self.finished.wait(timeout)


def test_cancel_queued_run_never_starts_driver(queue_system: MockSystem):
    first = _StreamThread(queue_system, body(task="task-1")).start()
    second = _StreamThread(queue_system, body(task="task-2")).start()
    try:
        with queue_system.client() as client:
            # The queued run has no events at all.
            events = client.get(f"/api/v1/runs/{second.run_id}/events").json()["events"]
            assert events == []
            cancel = client.post(f"/api/v1/runs/{second.run_id}/cancel")
            assert cancel.status_code == 200
            payload = cancel.json()
            assert payload["requested"] is True
            assert payload["confirmed"] is True
            assert "driver start" in payload["detail"]
            second_run = client.get(f"/api/v1/runs/{second.run_id}").json()
            assert second_run["status"] == "cancelled"
            assert second_run["artifacts"] == []

            first_run = client.get(f"/api/v1/runs/{first.run_id}").json()
            assert first_run["status"] in {"queued", "starting", "running"}
            client.post(f"/api/v1/runs/{first.run_id}/cancel")
    finally:
        first.join()
        second.join()
    assert second.error is None


def test_cancel_running_reports_requested_and_confirmed(long_hang_system: MockSystem):
    stream = _StreamThread(long_hang_system, body()).start()
    with long_hang_system.client() as client:
        cancel = client.post(f"/api/v1/runs/{stream.run_id}/cancel")
        assert cancel.status_code == 200
        payload = cancel.json()
        assert payload["requested"] is True
        assert payload["confirmed"] is True
        run = client.get(f"/api/v1/runs/{stream.run_id}").json()
        assert run["status"] == "cancelled"
    stream.join()


def test_hang_hits_finite_deadline_not_completed(hang_system: MockSystem):
    started = time.monotonic()
    with hang_system.client() as client:
        response = client.post(CHAT, json=body())
    elapsed = time.monotonic() - started
    assert elapsed < 10.0
    assert response.status_code in (409, 502)
    run = response.json()["run"]
    assert run["status"] in {"cancelled", "unknown"}
    assert run["outcome"] != "succeeded"


def test_unknown_run_quarantines_and_blocks_other_tasks(unknown_system: MockSystem):
    with unknown_system.client() as client:
        first = client.post(CHAT, json=body(task="task-1"))
        assert first.status_code == 502
        assert first.json()["run"]["status"] == "unknown"
        assert first.json()["error"]["code"] == "run_unknown"

        # A different task on the quarantined runner is refused before dispatch.
        second = client.post(CHAT, json=body(task="task-2", content="other"))
        assert second.status_code == 503
        assert second.json()["error"]["code"] == "runner_quarantined"

        # The unknown attempt is not retried.
        retry = client.post(CHAT, json=body(task="task-1"))
        assert retry.status_code == 409
        assert retry.json()["error"]["code"] == "unknown_attempt"


def _raw_sse_disconnect(system: MockSystem, payload: dict) -> str:
    """Open a real SSE socket, read the run id, then abruptly close it."""
    import socket as _socket

    def run_id_from(headers: str) -> str:
        for line in headers.split("\r\n"):
            if line.lower().startswith("x-run-id:"):
                return line.split(":", 1)[1].strip()
        raise AssertionError(f"no run id in headers: {headers!r}")

    data = json.dumps({**payload, "stream": True}).encode()
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Authorization: Bearer local-alpha-key\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(data)}\r\n\r\n".encode()
        + data
    )
    sock = _socket.create_connection(("127.0.0.1", system.port), timeout=10)
    try:
        sock.sendall(request)
        received = sock.recv(4096).decode("utf-8", "replace")
        assert "200" in received.split("\r\n")[0]
        return run_id_from(received.split("\r\n\r\n", 1)[0])
    finally:
        sock.close()


def test_disconnect_does_not_untrack_or_rerun(long_hang_system: MockSystem):
    run_id = _raw_sse_disconnect(long_hang_system, body(task="task-disc"))
    # The client is gone; the tracked run must remain queryable and not rerun.
    with long_hang_system.client() as client:
        deadline = time.time() + 10
        status = None
        while time.time() < deadline:
            run = client.get(f"/api/v1/runs/{run_id}")
            assert run.status_code == 200
            status = run.json()["status"]
            if status in {"starting", "running"}:
                break
            time.sleep(0.05)
        assert status in {"queued", "starting", "running"}
        # No new execution was silently created for the same task.
        duplicate = client.post(CHAT, json=body(task="task-disc"))
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] in {"run_active", "unknown_attempt"}
        cancel = client.post(f"/api/v1/runs/{run_id}/cancel")
        assert cancel.status_code == 200


def test_api_restart_marks_inflight_attempt_unknown(long_hang_system: MockSystem):
    stream = _StreamThread(long_hang_system, body(task="task-restart")).start()
    run_id = stream.run_id
    # Wait until the API has recorded it as running.
    deadline = time.time() + 10
    with long_hang_system.client() as client:
        while time.time() < deadline:
            status = client.get(f"/api/v1/runs/{run_id}").json()["status"]
            if status == "running":
                break
            time.sleep(0.05)
        assert status == "running"

    long_hang_system.restart_api()

    with long_hang_system.client() as client:
        run = client.get(f"/api/v1/runs/{run_id}").json()
        assert run["status"] == "unknown"
        # Not replayed: the logical lock is preserved.
        retry = client.post(CHAT, json=body(task="task-restart"))
        assert retry.status_code == 409
        assert retry.json()["error"]["code"] in {"unknown_attempt", "run_active"}
    stream.join(timeout=1.0)
