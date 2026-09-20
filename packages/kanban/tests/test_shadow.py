"""Shadow read: readonly board access, scope filter, cache, report."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from cli_provider_kanban.board import card_fingerprint, list_scope_tasks, open_readonly_board
from cli_provider_kanban.shadow import ShadowError, run_shadow

from conftest import (
    insert_task,
    policy_dict,
    spec_body,
    spec_dict,
    write_policy,
)


def _report(out_path):
    return json.loads(out_path.read_text(encoding="utf-8"))


def _board_dump(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tasks = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        links = conn.execute("SELECT * FROM task_links ORDER BY 1, 2").fetchall()
        events = conn.execute("SELECT * FROM task_events ORDER BY id").fetchall()
    finally:
        conn.close()
    return {"tasks": tasks, "links": links, "events": events}


def test_shadow_reads_and_writes_report(board_db, tmp_path):
    insert_task(board_db, task_id="t_abc12345", body=spec_body())
    insert_task(board_db, task_id="t_other", assignee="someone-else",
                body=spec_body(spec_dict(task_id="t_other")))
    policy = write_policy(tmp_path)
    out = tmp_path / "report.json"

    report = run_shadow(board_db=board_db, policy_path=policy, out_path=out)
    assert out.is_file()
    records = report["records"]
    assert [r["task_id"] for r in records] == ["t_abc12345"]
    assert records[0]["decision"]["recommended_action"] == "execute"
    assert records[0]["decision"]["route"] == "worker.code.standard"
    assert records[0]["provenance"] == "rules"
    assert report["assignee"] == "jev-native"
    assert report["policy_version"] == "2026-09-20.1"


def test_shadow_is_byte_stable_and_writes_nothing_to_board(board_db, tmp_path):
    insert_task(board_db, task_id="t_abc12345", body=spec_body())
    before_bytes = board_db.read_bytes()
    before_dump = _board_dump(board_db)
    before_files = {
        f for f in os.listdir(board_db.parent) if f.startswith(board_db.name)
    }

    run_shadow(
        board_db=board_db,
        policy_path=write_policy(tmp_path),
        out_path=tmp_path / "report.json",
    )

    assert board_db.read_bytes() == before_bytes
    assert _board_dump(board_db) == before_dump
    # No WAL/SHM/init/dispatch lock artefacts from the shadow reader.
    after_files = {
        f for f in os.listdir(board_db.parent) if f.startswith(board_db.name)
    }
    assert after_files == before_files


def test_shadow_missing_board_never_created(tmp_path):
    missing = tmp_path / "boards" / "kanban.db"
    with pytest.raises(ShadowError):
        run_shadow(
            board_db=missing,
            policy_path=write_policy(tmp_path),
            out_path=tmp_path / "report.json",
        )
    assert not missing.exists()


def test_second_run_reuses_cache_without_duplicates(board_db, tmp_path):
    insert_task(board_db, task_id="t_abc12345", body=spec_body())
    insert_task(board_db, task_id="t_def67890", body=spec_body(spec_dict(task_id="t_def67890")))
    policy = write_policy(tmp_path)
    out = tmp_path / "report.json"
    cache = tmp_path / "cache.json"

    run_shadow(board_db=board_db, policy_path=policy, out_path=out, cache_path=cache)
    again = run_shadow(board_db=board_db, policy_path=policy, out_path=out, cache_path=cache)

    ids = [r["task_id"] for r in again["records"]]
    assert sorted(ids) == ["t_abc12345", "t_def67890"]
    assert len(ids) == len(set(ids))
    assert {r["provenance"] for r in again["records"]} == {"cache"}
    assert all(r["decision"]["recommended_action"] == "execute" for r in again["records"])


def test_mutated_card_same_revision_is_rejected(board_db, tmp_path):
    insert_task(board_db, task_id="t_abc12345", body=spec_body())
    policy = write_policy(tmp_path)
    cache = tmp_path / "cache.json"
    run_shadow(board_db=board_db, policy_path=policy,
               out_path=tmp_path / "r1.json", cache_path=cache)

    # Same declared revision, different content.
    mutated = spec_dict()
    mutated["objective"] = "Completely different objective"
    conn = sqlite3.connect(board_db)
    conn.execute("UPDATE tasks SET body = ? WHERE id = 't_abc12345'",
                 (spec_body(mutated),))
    conn.commit()
    conn.close()

    report = run_shadow(board_db=board_db, policy_path=policy,
                        out_path=tmp_path / "r2.json", cache_path=cache)
    record = report["records"][0]
    assert record["provenance"] == "conflict"
    assert record["decision"]["recommended_action"] == "replan"
    assert "revision" in record["decision"]["reason"].lower()


def test_bumped_revision_reclassifies(board_db, tmp_path):
    insert_task(board_db, task_id="t_abc12345", body=spec_body())
    policy = write_policy(tmp_path)
    cache = tmp_path / "cache.json"
    run_shadow(board_db=board_db, policy_path=policy,
               out_path=tmp_path / "r1.json", cache_path=cache)

    v2 = spec_dict(task_revision="2")
    conn = sqlite3.connect(board_db)
    conn.execute("UPDATE tasks SET body = ? WHERE id = 't_abc12345'",
                 (spec_body(v2),))
    conn.commit()
    conn.close()

    report = run_shadow(board_db=board_db, policy_path=policy,
                        out_path=tmp_path / "r2.json", cache_path=cache)
    record = report["records"][0]
    assert record["provenance"] == "rules"
    assert record["decision"]["task_revision"] == "2"


def test_policy_version_change_reclassifies(board_db, tmp_path):
    insert_task(board_db, task_id="t_abc12345", body=spec_body())
    cache = tmp_path / "cache.json"
    run_shadow(board_db=board_db, policy_path=write_policy(tmp_path),
               out_path=tmp_path / "r1.json", cache_path=cache)
    v2 = policy_dict(policy_version="2026-09-20.2")
    report = run_shadow(board_db=board_db, policy_path=write_policy(tmp_path, v2),
                        out_path=tmp_path / "r2.json", cache_path=cache)
    assert report["records"][0]["provenance"] == "rules"
    assert report["policy_version"] == "2026-09-20.2"


def test_max_cards_limit(board_db, tmp_path):
    for i in range(3):
        insert_task(board_db, task_id=f"t_{i:08d}", body=spec_body(spec_dict(task_id=f"t_{i:08d}")))
    data = policy_dict()
    data["limits"]["max_cards"] = 2
    with pytest.raises(ShadowError, match="max_cards"):
        run_shadow(board_db=board_db, policy_path=write_policy(tmp_path, data),
                   out_path=tmp_path / "report.json")


def test_scope_statuses_filter(board_db, tmp_path):
    insert_task(board_db, task_id="t_abc12345", body=spec_body(), status="done")
    insert_task(board_db, task_id="t_def67890", body=spec_body(spec_dict(task_id="t_def67890")))
    report = run_shadow(board_db=board_db, policy_path=write_policy(tmp_path),
                        out_path=tmp_path / "report.json")
    assert [r["task_id"] for r in report["records"]] == ["t_def67890"]


def test_readonly_connection_refuses_writes(board_db):
    conn = open_readonly_board(board_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE tasks SET status='done'")
    finally:
        conn.close()


def test_list_scope_tasks_and_fingerprint(board_db):
    insert_task(board_db, task_id="t_abc12345", body=spec_body())
    conn = open_readonly_board(board_db)
    try:
        tasks = list_scope_tasks(conn, assignee="jev-native",
                                 statuses=("ready", "todo"), limit=10)
    finally:
        conn.close()
    assert len(tasks) == 1
    assert tasks[0].id == "t_abc12345"
    fp1 = card_fingerprint(tasks[0])

    conn2 = sqlite3.connect(board_db)
    conn2.execute("UPDATE tasks SET title = 'changed' WHERE id = 't_abc12345'")
    conn2.commit()
    conn2.close()
    conn = open_readonly_board(board_db)
    try:
        tasks2 = list_scope_tasks(conn, assignee="jev-native",
                                  statuses=("ready", "todo"), limit=10)
    finally:
        conn.close()
    assert card_fingerprint(tasks2[0]) != fp1


def test_open_readonly_requires_existing_db(tmp_path):
    with pytest.raises(ShadowError):
        open_readonly_board(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


def test_records_carry_fingerprint(board_db, tmp_path):
    insert_task(board_db, task_id="t_abc12345", body=spec_body())
    report = run_shadow(board_db=board_db, policy_path=write_policy(tmp_path),
                        out_path=tmp_path / "report.json")
    assert report["records"][0]["fingerprint"].startswith("sha256:")
