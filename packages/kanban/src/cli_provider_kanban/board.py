"""Read-only access to a Hermes Kanban SQLite board.

The shadow reader NEVER uses ``hermes_cli.kanban_db.connect``: that function
creates a missing DB, runs schema migrations and takes init/dispatch locks.
Here we open ``file:...?mode=ro`` only — a missing file is an error, no lock
files are created, and any write attempt raises ``sqlite3.OperationalError``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .errors import ShadowError

# Spec-bearing columns only; claim/run bookkeeping is deliberately excluded so
# a dispatcher's claim or heartbeat never invalidates a cached classification.
_SELECT_COLUMNS = (
    "id", "title", "body", "assignee", "status", "priority", "created_at",
    "workspace_kind", "workspace_path", "branch_name", "project_id", "tenant",
    "model_override", "provider_override", "reasoning_effort", "skills",
    "max_retries", "max_runtime_seconds",
)


@dataclass(frozen=True)
class BoardTask:
    """Immutable view of one ``tasks`` row plus its parent links."""

    id: str
    title: str
    body: str | None
    assignee: str | None
    status: str
    priority: int
    created_at: int
    workspace_kind: str
    workspace_path: str | None
    branch_name: str | None = None
    project_id: str | None = None
    tenant: str | None = None
    model_override: str | None = None
    provider_override: str | None = None
    reasoning_effort: str | None = None
    skills: str | None = None
    max_retries: int | None = None
    max_runtime_seconds: int | None = None
    parents: tuple[str, ...] = field(default_factory=tuple)


def open_readonly_board(db_path: Path | str) -> sqlite3.Connection:
    """Open ``db_path`` with SQLite ``mode=ro``. Never creates the file."""
    path = Path(db_path)
    if not path.is_file():
        raise ShadowError(f"board DB does not exist: {path}")
    try:
        conn = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None
        )
    except sqlite3.Error as exc:
        raise ShadowError(f"cannot open board DB read-only at {path}: {exc}") from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise ShadowError(f"cannot read board DB at {path}: {exc}") from exc
    if row is None:
        conn.close()
        raise ShadowError(f"{path} is not an initialized kanban board (no tasks table)")
    return conn


def _parents(conn: sqlite3.Connection, task_id: str) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    return tuple(str(r[0]) for r in rows)


def list_scope_tasks(
    conn: sqlite3.Connection,
    *,
    assignee: str,
    statuses: Iterable[str],
    limit: int,
) -> list[BoardTask]:
    """In-scope cards in board dispatch order (priority DESC, created_at ASC).

    Raises :class:`ShadowError` when more than ``limit`` cards match so a
    misconfigured scope cannot produce an unbounded report.
    """
    status_list = [str(s) for s in statuses]
    if not status_list:
        return []
    marks = ",".join("?" for _ in status_list)
    rows = conn.execute(
        f"SELECT {', '.join(_SELECT_COLUMNS)} FROM tasks "
        f"WHERE assignee = ? AND status IN ({marks}) "
        "ORDER BY priority DESC, created_at ASC",
        (assignee, *status_list),
    ).fetchall()
    if len(rows) > limit:
        raise ShadowError(
            f"scope matches {len(rows)} cards, over the max_cards limit of {limit}"
        )
    tasks = []
    for row in rows:
        data = {col: row[col] for col in _SELECT_COLUMNS}
        tasks.append(BoardTask(**data, parents=_parents(conn, data["id"])))
    return tasks


def card_fingerprint(task: BoardTask) -> str:
    """SHA-256 over the card's spec-bearing fields (canonical JSON).

    Covers everything that changes *what the card asks for* — title, body,
    assignee, workspace/branch, model/provider/effort overrides, skills,
    retry/runtime bounds and declared parents — while excluding lifecycle
    bookkeeping (status, claim, run pointers).
    """
    payload = {
        "id": task.id,
        "title": task.title,
        "body": task.body,
        "assignee": task.assignee,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "branch_name": task.branch_name,
        "project_id": task.project_id,
        "model_override": task.model_override,
        "provider_override": task.provider_override,
        "reasoning_effort": task.reasoning_effort,
        "skills": task.skills,
        "max_retries": task.max_retries,
        "max_runtime_seconds": task.max_runtime_seconds,
        "parents": list(task.parents),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
