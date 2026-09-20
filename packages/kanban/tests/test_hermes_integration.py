"""Tests against the actually-installed Hermes kanban APIs.

A scratch ``HERMES_HOME``/``HERMES_KANBAN_DB`` inside ``tmp_path`` is the only
target; the real profile tree is never touched. These tests are skipped when
the installed Hermes venv is not discoverable.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from cli_provider_kanban.shadow import run_shadow

from conftest import (
    requires_hermes,
    run_hermes,
    spec_body,
    spec_dict,
    write_policy,
)

pytestmark = requires_hermes


def _hermes_create(tmp_path, body, assignee="jev-native"):
    """Create one card on a scratch board via the real Hermes API."""
    proc = run_hermes(tmp_path, """
import json
from pathlib import Path
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
conn = kbc.connect(Path(%(db)r))
tid = kb.create_task(conn, title="jev card", body=%(body)r,
                     assignee=%(assignee)r, workspace_kind="scratch")
conn.close()
print("RESULT " + json.dumps({"task_id": tid}))
""" % {
        "db": str(tmp_path / "kanban.db"),
        "body": body,
        "assignee": assignee,
    })
    assert proc.returncode == 0, proc.stderr
    line = [l for l in proc.stdout.splitlines() if l.startswith("RESULT ")][-1]
    return json.loads(line[len("RESULT "):])["task_id"]


def _hermes_update_body(tmp_path, task_id, body):
    proc = run_hermes(tmp_path, """
from pathlib import Path
from hermes_cli import kanban_db_connect as kbc
conn = kbc.connect(Path(%(db)r))
conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (%(body)r, %(tid)r))
conn.commit()
conn.close()
print("RESULT ok")
""" % {"db": str(tmp_path / "kanban.db"), "body": body, "tid": task_id})
    assert proc.returncode == 0, proc.stderr


def test_real_board_created_and_shadowed(tmp_path):
    task_id = _hermes_create(tmp_path, "triage text")
    _hermes_update_body(tmp_path, task_id, spec_body(spec_dict(task_id=task_id)))

    report = run_shadow(
        board_db=tmp_path / "kanban.db",
        policy_path=write_policy(tmp_path),
        out_path=tmp_path / "report.json",
    )
    assert report["records"][0]["task_id"] == task_id
    assert report["records"][0]["decision"]["recommended_action"] == "execute"


def test_stock_dispatch_skips_external_assignee_no_side_effects(tmp_path):
    task_id = _hermes_create(tmp_path, spec_body(spec_dict(task_id="t_unused")))

    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    before = conn.execute(
        "SELECT status, claim_lock FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    before_events = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    conn.close()

    proc = run_hermes(tmp_path, """
import json
from pathlib import Path
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

conn = kbc.connect(Path(%r))
calls = []
def spawn_fn(task, workspace, **kw):
    calls.append(task.id)
    return None
result = kbd.dispatch_once(conn, spawn_fn=spawn_fn)
conn.close()
print("RESULT " + json.dumps({
    "spawned": result.spawned,
    "skipped_nonspawnable": result.skipped_nonspawnable,
    "calls": calls,
}))
""" % str(db))
    assert proc.returncode == 0, proc.stderr
    outcome = json.loads(
        [l for l in proc.stdout.splitlines() if l.startswith("RESULT ")][-1][7:]
    )
    assert outcome["calls"] == []
    assert outcome["spawned"] == []
    assert task_id in outcome["skipped_nonspawnable"]

    conn = sqlite3.connect(db)
    after = conn.execute(
        "SELECT status, claim_lock FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    after_events = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
    ).fetchone()[0]
    conn.close()
    assert after == before == ("ready", None)
    assert after_events == before_events


def test_shadow_never_mutates_real_board(tmp_path):
    task_id = _hermes_create(tmp_path, spec_body(spec_dict(task_id="t_unused")))
    db = tmp_path / "kanban.db"
    before_bytes = db.read_bytes()

    run_shadow(
        board_db=db,
        policy_path=write_policy(tmp_path),
        out_path=tmp_path / "report.json",
    )
    assert db.read_bytes() == before_bytes


def test_foreign_assignee_out_of_scope(tmp_path):
    _hermes_create(tmp_path, "plain", assignee="someone-else")

    report = run_shadow(
        board_db=tmp_path / "kanban.db",
        policy_path=write_policy(tmp_path),
        out_path=tmp_path / "report.json",
    )
    assert report["records"] == []
