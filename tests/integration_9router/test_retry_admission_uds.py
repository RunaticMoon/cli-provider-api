"""Retry-safety verticals through the real API -> UDS -> worker fixture.

These tests use no gateway: they drive the API directly and prove the durable
cross-candidate admission guard against the real Runner-protocol boundary.
``runs.ndjson`` counts every ``run`` RPC the fixture received;
``effects.ndjson`` counts every agent start — the synthetic file effect that
must never happen twice for one task.
"""

from __future__ import annotations

import json
import threading
import time

from conftest import CHAT, EXECUTION, FixtureSystem, chat_body


def test_preflight_rejection_admits_fallback_candidate_once(fixture_factory):
    """Positive control: A is rejected before dispatch, B runs exactly once."""
    system = fixture_factory(
        plan={"presets": {"fixture/alpha": ["reject", "success"]}}
    )
    with system.client() as client:
        first = client.post(CHAT, json=chat_body(task="task-pf"))
        assert first.status_code == 502
        assert first.json()["error"]["code"] == "run_rejected"
        assert first.json()["run"]["outcome"] == "rejected"
        rejected_id = first.json()["run"]["run_id"]

        # The proven pre-execution rejection admits one new attempt.
        second = client.post(CHAT, json=chat_body(task="task-pf"))
        assert second.status_code == 200
        run = second.json()["run"]
        assert run["status"] == "completed" and run["run_id"] != rejected_id

    received = system.runs_received()
    assert len(received) == 2
    assert received[0]["dispatched"] is False
    assert received[1]["dispatched"] is True
    # Exactly one agent start — A produced no fixture file effect.
    assert len(system.effects()) == 1
    assert system.effects()[0]["run_id"] == run["run_id"]


def test_effectful_failure_never_reruns_even_after_restart(fixture_factory):
    """A modified the fixture file then failed: zero second agent starts,
    the original run identity stays readable, and restart changes nothing."""
    system = fixture_factory(plan={"default": "fail"})
    with system.client() as client:
        first = client.post(CHAT, json=chat_body(task="task-eff"))
        assert first.status_code == 502
        run = first.json()["run"]
        assert run["status"] == "failed" and run["outcome"] == "provider_error"
        original = run["run_id"]

        # Same model and cross-model retries are both blocked.
        for model in ("fixture/alpha", "fixture/beta"):
            retry = client.post(CHAT, json=chat_body(task="task-eff", model=model))
            assert retry.status_code == 409
            assert retry.json()["error"]["code"] == "run_not_retryable"
            assert retry.json()["error"]["run_id"] == original

        assert client.get(f"/api/v1/runs/{original}").json()["run_id"] == original

    system.restart_api()

    with system.client() as client:
        retry = client.post(CHAT, json=chat_body(task="task-eff"))
        assert retry.status_code == 409
        assert retry.json()["error"]["code"] == "run_not_retryable"

    assert len(system.effects()) == 1
    assert len(system.runs_received()) == 1


def test_dropped_response_after_agent_start_is_never_retried(fixture_factory):
    """Response dropped after agent start/before terminal: unknown is held."""
    system = fixture_factory(
        plan={"default": "drop"},
        config_overrides={
            "api": {
                "default_run_deadline_seconds": 2.0,
                "max_run_deadline_seconds": 4.0,
                "cancel_deadline_seconds": 0.5,
            }
        },
    )
    with system.client() as client:
        first = client.post(CHAT, json=chat_body(task="task-drop"))
        assert first.status_code == 502
        run = first.json()["run"]
        assert run["status"] == "unknown"
        assert first.json()["error"]["code"] == "run_unknown"

        # Uncertainty is never a retry admission, under any preset.
        retry = client.post(CHAT, json=chat_body(task="task-drop", model="fixture/beta"))
        assert retry.status_code == 409
        assert retry.json()["error"]["code"] in {"unknown_attempt", "run_not_retryable"}

        # The runner is quarantined for other tasks too.
        other = client.post(CHAT, json=chat_body(task="task-other"))
        assert other.status_code == 503
        assert other.json()["error"]["code"] == "runner_quarantined"

    assert len(system.effects()) == 1
    assert len(system.runs_received()) == 1


def test_transport_error_after_first_frame_is_never_retried(fixture_factory):
    system = fixture_factory(
        plan={"default": "error_after_event"},
        config_overrides={
            "api": {
                "default_run_deadline_seconds": 2.0,
                "max_run_deadline_seconds": 4.0,
                "cancel_deadline_seconds": 0.5,
            }
        },
    )
    with system.client() as client:
        first = client.post(CHAT, json=chat_body(task="task-err"))
        assert first.status_code == 502
        assert first.json()["run"]["status"] == "unknown"
        retry = client.post(CHAT, json=chat_body(task="task-err"))
        assert retry.status_code == 409
        assert retry.json()["error"]["code"] in {"unknown_attempt", "run_not_retryable"}
    assert len(system.effects()) == 1


def test_dispatched_cancel_is_never_retried(fixture_factory):
    """A cancel that lands after dispatch confirms cancelled; the attempt ran,
    so the task can never admit a new attempt afterwards."""
    system = fixture_factory(plan={"default": "hang"})
    holder = _StreamThread(system, chat_body(task="task-late", stream=True)).start()
    try:
        with system.client() as client:
            cancel = client.post(f"/api/v1/runs/{holder.run_id}/cancel")
            assert cancel.status_code == 200
            deadline = time.time() + 10
            status = None
            while time.time() < deadline:
                status = client.get(f"/api/v1/runs/{holder.run_id}").json()["status"]
                if status in {"cancelled", "unknown"}:
                    break
                time.sleep(0.05)
            assert status in {"cancelled", "unknown"}
            retry = client.post(CHAT, json=chat_body(task="task-late"))
            assert retry.status_code == 409
            assert retry.json()["error"]["code"] in {
                "run_not_retryable",
                "unknown_attempt",
            }
    finally:
        holder.join()
    assert len(system.effects()) == 1


def test_concurrent_duplicate_executes_once(fixture_factory):
    system = fixture_factory(plan={"default": "hang"})
    holder = _StreamThread(system, chat_body(task="task-dup", stream=True)).start()
    try:
        with system.client() as client:
            duplicate = client.post(CHAT, json=chat_body(task="task-dup"))
            assert duplicate.status_code == 409
            error = duplicate.json()["error"]
            assert error["code"] in {"run_active", "run_not_retryable"}
            assert error["run_id"] == holder.run_id
            client.post(f"/api/v1/runs/{holder.run_id}/cancel")
    finally:
        holder.join()
    assert len(system.effects()) == 1


def test_partial_result_is_cached_not_retried(fixture_factory):
    system = fixture_factory(plan={"default": "partial"})
    with system.client() as client:
        first = client.post(CHAT, json=chat_body(task="task-part"))
        assert first.status_code == 200
        run = first.json()["run"]
        assert run["status"] == "completed" and run["outcome"] == "partial"
        replay = client.post(CHAT, json=chat_body(task="task-part"))
        assert replay.status_code == 200
        assert replay.json()["run"]["run_id"] == run["run_id"]
        assert replay.json()["run"]["cached"] is True
    assert len(system.effects()) == 1


def test_execution_context_reaches_worker_and_result(fixture_system: FixtureSystem):
    """metadata.execution propagates API -> Store -> UDS -> worker fixture and
    is echoed back on the normalized run view; run/attempt ids stay
    wrapper-generated."""
    with fixture_system.client() as client:
        response = client.post(
            CHAT, json=chat_body(task="task-meta", execution=EXECUTION)
        )
        assert response.status_code == 200
        run = response.json()["run"]
        assert run["execution"] == EXECUTION
        assert run["run_id"].startswith("run_")
        assert run["attempt_id"].startswith("att_")

    received = fixture_system.runs_received()
    assert len(received) == 1
    entry = received[0]
    assert entry["execution"] == EXECUTION
    assert entry["run_id"] == run["run_id"]
    assert entry["attempt_id"] == run["attempt_id"]
    assert entry["task_id"] == "task-meta"
    assert fixture_system.effects()[0]["execution"] == EXECUTION


def test_execution_context_conflict_and_replay(fixture_system: FixtureSystem):
    with fixture_system.client() as client:
        first = client.post(
            CHAT, json=chat_body(task="task-meta2", execution=EXECUTION)
        )
        assert first.status_code == 200
        run_id = first.json()["run"]["run_id"]

        # Identical replay (same context) returns the cached original.
        replay = client.post(
            CHAT, json=chat_body(task="task-meta2", execution=EXECUTION)
        )
        assert replay.json()["run"]["run_id"] == run_id
        assert replay.json()["run"]["cached"] is True

        # Same task id, mutated context: a different logical request.
        mutated = dict(EXECUTION, task_revision="rev-8")
        conflict = client.post(
            CHAT, json=chat_body(task="task-meta2", execution=mutated)
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "task_content_conflict"
    assert len(fixture_system.effects()) == 1


def test_no_execution_tool_calls_in_response(fixture_system: FixtureSystem):
    with fixture_system.client() as client:
        response = client.post(CHAT, json=chat_body(task="task-tools"))
        assert response.status_code == 200
        choice = response.json()["choices"][0]["message"]
        assert "tool_calls" not in choice
        run = response.json()["run"]
        assert "tool_calls" not in json.dumps(run)


class _StreamThread:
    """Opens an SSE request in a thread and records its run id promptly."""

    def __init__(self, system: FixtureSystem, payload: dict) -> None:
        self.system = system
        self.payload = payload
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
            with self.system.client() as client:
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


def test_streaming_run_survives_and_is_never_duplicated(fixture_system: FixtureSystem):
    """SSE before and after the first frame; the streamed run is the only
    execution and replays cached rather than re-running."""
    stream = _StreamThread(
        fixture_system, chat_body(task="task-sse", stream=True)
    ).start()
    stream.join()
    assert stream.error is None
    frames = [
        line[6:] for line in stream.body.splitlines() if line.startswith("data: ")
    ]
    assert "[DONE]" in frames
    objects = [json.loads(line) for line in frames if line != "[DONE]"]
    assert not any("error" in value for value in objects)
    first_run = objects[0]["run"]
    final_run = objects[-1]["run"]
    assert first_run["run_id"] == final_run["run_id"] == stream.run_id
    assert final_run["status"] == "completed"

    with fixture_system.client() as client:
        replay = client.post(CHAT, json=chat_body(task="task-sse"))
        assert replay.json()["run"]["run_id"] == stream.run_id
        assert replay.json()["run"]["cached"] is True
    assert len(fixture_system.effects()) == 1
