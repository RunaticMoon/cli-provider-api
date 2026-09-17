"""SQLite persistence for tasks, attempts, events, artifacts and quarantine.

Single API instance, synchronous stdlib sqlite3 guarded by an in-process lock.
Writes are small and local; correctness (atomic reserve, unique logical lock,
ordered events) matters more than raw throughput here.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Sequence

from .errors import Conflict, UpstreamProtocolError
from .models import (
    ACTIVE_STATUSES,
    LOCK_STATUSES,
    AttemptRecord,
    ArtifactRecord,
    EventRecord,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    principal TEXT NOT NULL,
    task_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    task_policy TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (principal, task_id)
);
CREATE TABLE IF NOT EXISTS attempts (
    run_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE,
    preset TEXT NOT NULL,
    driver_id TEXT NOT NULL,
    runner_instance TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    summary TEXT,
    detail TEXT,
    verification TEXT,
    usage TEXT,
    cached INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS attempts_logical_lock
    ON attempts(principal, task_id)
    WHERE status IN ('reserved','queued','starting','running','cancelling','unknown');
CREATE INDEX IF NOT EXISTS attempts_task
    ON attempts(principal, task_id, created_at);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 0,
    timestamp TEXT NOT NULL,
    PRIMARY KEY (run_id, sequence)
);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    principal TEXT NOT NULL,
    preset TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_owner ON artifacts(principal, run_id);
CREATE TABLE IF NOT EXISTS runner_quarantine (
    instance_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    since TEXT NOT NULL,
    run_id TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lock_placeholders(statuses: Sequence[str]) -> str:
    return ",".join("?" for _ in statuses)


class Store:
    def __init__(self, db_path: str, *, clock=utcnow) -> None:
        self.db_path = db_path
        self._clock = clock
        self._lock = threading.RLock()
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    # ------------------------------------------------------------ lifecycle

    def initialize(self, *, reconcile: bool = True) -> dict[str, Any]:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            if reconcile:
                return self._reconcile_restart()
        return {"reconciled": 0}

    def _reconcile_restart(self) -> dict[str, Any]:
        """Formerly active attempts become unknown; never re-queued for replay."""
        now = self._clock()
        placeholders = _lock_placeholders(sorted(ACTIVE_STATUSES))
        cursor = self._conn.execute(
            f"UPDATE attempts SET status='unknown', outcome='unknown', "
            f"finished_at=?, updated_at=?, detail=? WHERE status IN ({placeholders})",
            (now, now, "api restarted during execution; not reconciled", *sorted(ACTIVE_STATUSES)),
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('last_reconcile', ?)", (now,)
        )
        return {"reconciled": cursor.rowcount, "at": now}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- tasks

    def get_task(self, principal: str, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE principal=? AND task_id=?", (principal, task_id)
            ).fetchone()
        return dict(row) if row else None

    def latest_attempt(self, principal: str, task_id: str) -> AttemptRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM attempts WHERE principal=? AND task_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (principal, task_id),
            ).fetchone()
        return _attempt(row) if row else None

    def list_attempts(self, principal: str, task_id: str) -> list[AttemptRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM attempts WHERE principal=? AND task_id=? "
                "ORDER BY created_at DESC, rowid DESC",
                (principal, task_id),
            ).fetchall()
        return [_attempt(r) for r in rows]

    def reserve(
        self,
        *,
        run_id: str,
        attempt_id: str,
        principal: str,
        task_id: str,
        preset: str,
        driver_id: str,
        runner_instance: str,
        workspace_id: str,
        request_hash: str,
        task_policy: str,
        status: str,
        outcome: str | None = None,
        detail: str | None = None,
        summary: str | None = None,
        cached: bool = False,
        finished_at: str | None = None,
    ) -> AttemptRecord:
        """Atomically reserve a task+attempt before any run dispatch.

        Re-checks the logical lock and content hash inside the transaction so
        concurrent identical calls cannot both execute.
        """
        now = self._clock()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                task = self._conn.execute(
                    "SELECT * FROM tasks WHERE principal=? AND task_id=?",
                    (principal, task_id),
                ).fetchone()
                if task is not None and task["request_hash"] != request_hash:
                    raise Conflict(
                        "task_id already exists with different content",
                        code="task_content_conflict",
                    )
                locked = self._conn.execute(
                    f"SELECT run_id, status FROM attempts WHERE principal=? AND task_id=? "
                    f"AND status IN ({_lock_placeholders(sorted(LOCK_STATUSES))}) LIMIT 1",
                    (principal, task_id, *sorted(LOCK_STATUSES)),
                ).fetchone()
                if locked is not None:
                    raise Conflict(
                        "another attempt is active or unknown for this task",
                        code="run_active",
                        run_id=locked["run_id"],
                    )
                if task is None:
                    self._conn.execute(
                        "INSERT INTO tasks(principal, task_id, request_hash, task_policy, "
                        "workspace_id, created_at) VALUES(?,?,?,?,?,?)",
                        (principal, task_id, request_hash, task_policy, workspace_id, now),
                    )
                self._conn.execute(
                    "INSERT INTO attempts(run_id, principal, task_id, attempt_id, preset, "
                    "driver_id, runner_instance, workspace_id, request_hash, status, outcome, "
                    "detail, summary, cached, created_at, updated_at, finished_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id, principal, task_id, attempt_id, preset, driver_id,
                        runner_instance, workspace_id, request_hash, status, outcome,
                        detail, summary, int(cached), now, now, finished_at,
                    ),
                )
                self._conn.execute("COMMIT")
            except Conflict:
                self._conn.execute("ROLLBACK")
                raise
            except sqlite3.IntegrityError as exc:
                self._conn.execute("ROLLBACK")
                raise Conflict(
                    "another attempt is active or unknown for this task",
                    code="run_active",
                ) from exc
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        record = self.get_attempt(run_id)
        assert record is not None
        return record

    # -------------------------------------------------------------- attempts

    def get_attempt(self, run_id: str) -> AttemptRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM attempts WHERE run_id=?", (run_id,)
            ).fetchone()
        return _attempt(row) if row else None

    def set_attempt(
        self,
        run_id: str,
        *,
        status: str | None = None,
        outcome: str | None = None,
        detail: str | None = None,
        summary: str | None = None,
        verification: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        cached: bool | None = None,
    ) -> None:
        fields: list[str] = ["updated_at=?"]
        values: list[Any] = [self._clock()]
        if status is not None:
            fields.append("status=?")
            values.append(status)
        if outcome is not None:
            fields.append("outcome=?")
            values.append(outcome)
        if detail is not None:
            fields.append("detail=?")
            values.append(detail)
        if summary is not None:
            fields.append("summary=?")
            values.append(summary)
        if verification is not None:
            fields.append("verification=?")
            values.append(json.dumps(verification, sort_keys=True))
        if usage is not None:
            fields.append("usage=?")
            values.append(json.dumps(usage, sort_keys=True))
        if started_at is not None:
            fields.append("started_at=?")
            values.append(started_at)
        if finished_at is not None:
            fields.append("finished_at=?")
            values.append(finished_at)
        if cached is not None:
            fields.append("cached=?")
            values.append(int(cached))
        values.append(run_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE attempts SET {', '.join(fields)} WHERE run_id=?", values
            )

    # ---------------------------------------------------------------- events

    def append_event(self, run_id: str, event: dict[str, Any]) -> None:
        sequence = int(event["sequence"])
        row = (
            run_id,
            sequence,
            str(event["kind"]),
            json.dumps(event, sort_keys=True),
            int(bool(event.get("synthetic", False))),
            str(event["timestamp"]),
        )
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO events(run_id, sequence, kind, payload, synthetic, timestamp) "
                    "VALUES(?,?,?,?,?,?)",
                    row,
                )
            except sqlite3.IntegrityError as exc:
                raise UpstreamProtocolError(
                    "duplicate or out-of-order run event"
                ) from exc

    def list_events(
        self, run_id: str, *, after: int = 0, limit: int = 1000
    ) -> list[EventRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE run_id=? AND sequence>? "
                "ORDER BY sequence ASC LIMIT ?",
                (run_id, after, limit),
            ).fetchall()
        return [
            EventRecord(
                run_id=r["run_id"],
                sequence=r["sequence"],
                kind=r["kind"],
                event=json.loads(r["payload"]),
                timestamp=r["timestamp"],
            )
            for r in rows
        ]

    def count_events(self, run_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE run_id=?", (run_id,)
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------- artifacts

    def add_artifact(self, record: ArtifactRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO artifacts(artifact_id, run_id, principal, preset, workspace_id, "
                "kind, content_type, size, sha256, path, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.artifact_id, record.run_id, record.principal, record.preset,
                    record.workspace_id, record.kind, record.content_type, record.size,
                    record.sha256, record.path, record.created_at,
                ),
            )

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            return None
        return ArtifactRecord(
            artifact_id=row["artifact_id"],
            run_id=row["run_id"],
            principal=row["principal"],
            preset=row["preset"],
            workspace_id=row["workspace_id"],
            kind=row["kind"],
            content_type=row["content_type"],
            size=row["size"],
            sha256=row["sha256"],
            path=row["path"],
            created_at=row["created_at"],
        )

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at", (run_id,)
            ).fetchall()
        return [self.get_artifact(r["artifact_id"]) for r in rows]  # type: ignore[misc]

    # ------------------------------------------------------------ quarantine

    def quarantine_instance(self, instance_id: str, reason: str, run_id: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runner_quarantine(instance_id, reason, since, run_id) "
                "VALUES(?,?,?,?) ON CONFLICT(instance_id) DO UPDATE SET "
                "reason=excluded.reason, since=excluded.since, run_id=excluded.run_id",
                (instance_id, reason, self._clock(), run_id),
            )

    def get_quarantine(self, instance_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runner_quarantine WHERE instance_id=?", (instance_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_quarantine(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runner_quarantine ORDER BY since"
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_quarantine(self, instance_id: str) -> bool:
        """Operator-only release. Never called automatically."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM runner_quarantine WHERE instance_id=?", (instance_id,)
            )
        return cursor.rowcount > 0

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def _attempt(row: sqlite3.Row) -> AttemptRecord:
    return AttemptRecord(
        run_id=row["run_id"],
        principal=row["principal"],
        task_id=row["task_id"],
        attempt_id=row["attempt_id"],
        preset=row["preset"],
        driver_id=row["driver_id"],
        runner_instance=row["runner_instance"],
        workspace_id=row["workspace_id"],
        request_hash=row["request_hash"],
        status=row["status"],
        outcome=row["outcome"],
        summary=row["summary"],
        detail=row["detail"],
        verification=json.loads(row["verification"]) if row["verification"] else None,
        usage=json.loads(row["usage"]) if row["usage"] else None,
        cached=bool(row["cached"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
