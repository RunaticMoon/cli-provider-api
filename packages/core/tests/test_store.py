import pytest

from cli_provider_core import ArtifactRecord, Conflict, Store
from cli_provider_core.models import (
    COMPLETED,
    OUTCOME_SUCCEEDED,
    QUEUED,
    RUNNING,
    UNKNOWN,
)


def make_store(tmp_path) -> Store:
    store = Store(str(tmp_path / "core.db"))
    store.initialize()
    return store


def reserve(store: Store, run_id: str, *, task="task-1", hash_="sha256:a", status=QUEUED):
    return store.reserve(
        run_id=run_id,
        attempt_id=f"att_{run_id}",
        principal="alpha",
        task_id=task,
        preset="mock/text",
        driver_id="mock",
        runner_instance="runner-1",
        workspace_id="ws-alpha",
        request_hash=hash_,
        task_policy="text",
        status=status,
    )


def test_reserve_then_second_active_attempt_conflicts(tmp_path):
    store = make_store(tmp_path)
    reserve(store, "run_1")
    with pytest.raises(Conflict) as excinfo:
        reserve(store, "run_2")
    assert excinfo.value.code == "run_active"
    assert excinfo.value.run_id == "run_1"
    store.close()


def test_changed_content_under_same_task_conflicts(tmp_path):
    store = make_store(tmp_path)
    reserve(store, "run_1")
    store.set_attempt("run_1", status=COMPLETED, outcome=OUTCOME_SUCCEEDED)
    with pytest.raises(Conflict) as excinfo:
        reserve(store, "run_2", hash_="sha256:b")
    assert excinfo.value.code == "task_content_conflict"
    store.close()


def test_unknown_attempt_holds_the_logical_lock(tmp_path):
    store = make_store(tmp_path)
    reserve(store, "run_1", status=RUNNING)
    store.set_attempt("run_1", status=UNKNOWN, outcome="unknown")
    with pytest.raises(Conflict) as excinfo:
        reserve(store, "run_2")
    assert excinfo.value.code == "run_active"
    store.close()


def test_restart_marks_active_attempts_unknown_not_replayable(tmp_path):
    store = make_store(tmp_path)
    reserve(store, "run_1", status=RUNNING)
    store.close()

    reopened = make_store(tmp_path)
    record = reopened.get_attempt("run_1")
    assert record is not None
    assert record.status == UNKNOWN
    assert record.outcome == "unknown"
    assert reopened.get_meta("last_reconcile") is not None
    # Still locked: unknown is not queued for replay.
    with pytest.raises(Conflict):
        reserve(reopened, "run_2")
    reopened.close()


def test_events_are_ordered_and_duplicates_rejected(tmp_path):
    from cli_provider_core import UpstreamProtocolError

    store = make_store(tmp_path)
    reserve(store, "run_1")
    store.append_event(
        "run_1",
        {"sequence": 1, "kind": "run.started", "timestamp": "t", "payload": {}},
    )
    store.append_event(
        "run_1",
        {"sequence": 2, "kind": "message.delta", "timestamp": "t", "payload": {"text": "x"}},
    )
    events = store.list_events("run_1")
    assert [e.sequence for e in events] == [1, 2]
    assert store.count_events("run_1") == 2
    with pytest.raises(UpstreamProtocolError):
        store.append_event(
            "run_1",
            {"sequence": 2, "kind": "message.delta", "timestamp": "t", "payload": {}},
        )
    store.close()


def test_artifacts_roundtrip(tmp_path):
    store = make_store(tmp_path)
    record = ArtifactRecord(
        artifact_id="art_1",
        run_id="run_1",
        principal="alpha",
        preset="mock/text",
        workspace_id="ws-alpha",
        kind="text",
        content_type="text/plain",
        size=3,
        sha256="abc",
        path=str(tmp_path / "a.txt"),
        created_at="t",
    )
    store.add_artifact(record)
    assert store.get_artifact("art_1") == record
    assert store.list_artifacts("run_1") == [record]
    store.close()


def test_quarantine_is_persisted_and_only_cleared_manually(tmp_path):
    store = make_store(tmp_path)
    store.quarantine_instance("runner-1", "unknown terminal", "run_1")
    store.close()

    reopened = make_store(tmp_path)
    entry = reopened.get_quarantine("runner-1")
    assert entry is not None and entry["reason"] == "unknown terminal"
    # Re-initialising (as an API restart would) must not clear it.
    reopened.initialize()
    assert reopened.get_quarantine("runner-1") is not None
    assert reopened.clear_quarantine("runner-1") is True
    assert reopened.get_quarantine("runner-1") is None
    reopened.close()
