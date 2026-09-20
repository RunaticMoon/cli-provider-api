"""Sidecar dispatch receipts — the only state this package owns.

Kanban remains the canonical task state and the wrapper the canonical run
state; this SQLite store records *what the dispatcher did* so a crash or
restart can recover without replaying an execution that may have happened:

- ``reservations``: one row per dispatch attempt. A partial unique index
  enforces at most one *active or unknown* reservation per card and per
  workspace — ``unknown`` deliberately blocks re-dispatch forever because the
  execution may have started and must never be replayed.
- ``approvals``: durable approval records for gated operations. Apply is an
  atomic consume-once transition; expiry denies by default.

Nothing here schedules, retries or decides; it is a receipt ledger.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

# Reservation lifecycle. ``reserved`` is persisted BEFORE the kernel claim so a
# crash on either side of the claim boundary recovers deterministically:
# reserved-but-never-claimed aborts cleanly; claimed/submitted/completing
# crashes are ``unknown`` (execution may have started -> never replay).
RESERVED = "reserved"
CLAIMED = "claimed"
SUBMITTED = "submitted"
COMPLETING = "completing"
REVIEW = "review"
BLOCKED = "blocked"
ABORTED = "aborted"      # never reached execution — safe to dispatch again
CANCELLED = "cancelled"
FAILED = "failed"
UNKNOWN = "unknown"      # may have executed — permanently blocks re-dispatch

ACTIVE_STATES = (RESERVED, CLAIMED, SUBMITTED, COMPLETING)
BLOCKING_STATES = ACTIVE_STATES + (UNKNOWN,)
TERMINAL_STATES = (REVIEW, BLOCKED, ABORTED, CANCELLED, FAILED, UNKNOWN)

ALL_STATES = frozenset(
    ACTIVE_STATES + TERMINAL_STATES
)

APPROVAL_PENDING = "pending"
APPROVAL_APPLIED = "applied"
APPROVAL_DENIED = "denied"
APPROVAL_EXPIRED = "expired"
APPROVAL_CONSUMED = "consumed"  # applied AND burned by one reservation

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    dispatch_id        TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    task_revision      TEXT NOT NULL,
    spec_hash          TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    workspace_id       TEXT NOT NULL,
    base_revision      TEXT NOT NULL,
    route              TEXT NOT NULL,
    state              TEXT NOT NULL,
    run_id             TEXT,
    attempt_id         TEXT,
    kernel_run_id      INTEGER,
    branch             TEXT,
    worktree           TEXT,
    cancel_requested   INTEGER NOT NULL DEFAULT 0,
    detail             TEXT,
    evidence           TEXT,        -- JSON: sanitized durable evidence
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL
);
-- One active-or-unknown reservation per card, across restarts.
CREATE UNIQUE INDEX IF NOT EXISTS ux_reservations_live_task
    ON reservations(task_id)
    WHERE state IN ('reserved','claimed','submitted','completing','unknown');
-- Same-workspace concurrency max 1 while a reservation is live.
CREATE UNIQUE INDEX IF NOT EXISTS ux_reservations_live_workspace
    ON reservations(workspace_id)
    WHERE state IN ('reserved','claimed','submitted','completing','unknown');

CREATE TABLE IF NOT EXISTS approvals (
    approval_id    TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL,
    task_revision  TEXT NOT NULL,
    spec_hash      TEXT NOT NULL DEFAULT '',
    policy_fingerprint TEXT NOT NULL DEFAULT '',
    operation      TEXT NOT NULL,
    run_id         TEXT,
    allowed_actors TEXT NOT NULL,   -- JSON array of configured identities
    state          TEXT NOT NULL DEFAULT 'pending',
    expires_at     REAL NOT NULL,
    created_at     REAL NOT NULL,
    decided_at     REAL,
    decided_by     TEXT
);
"""

# Columns added after the initial schema — applied idempotently.
_MIGRATIONS = (
    "ALTER TABLE reservations ADD COLUMN evidence TEXT",
    "ALTER TABLE approvals ADD COLUMN spec_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE approvals ADD COLUMN policy_fingerprint "
    "TEXT NOT NULL DEFAULT ''",
)


class StoreError(Exception):
    """Sidecar ledger failure (uniqueness, unknown id, bad transition)."""


class ReservationExists(StoreError):
    """A live or unknown reservation already covers this card/workspace."""

    def __init__(self, task_id: str, workspace_id: str):
        super().__init__(
            f"a live or unknown reservation already exists for task "
            f"{task_id!r} or workspace {workspace_id!r} — not dispatching"
        )
        self.task_id = task_id
        self.workspace_id = workspace_id


class GrantMismatch(StoreError):
    """An applied approval no longer matches the exact dispatch scope —
    the reservation must not be created and the grant must not burn."""


@dataclass(frozen=True)
class Reservation:
    dispatch_id: str
    task_id: str
    task_revision: str
    spec_hash: str
    policy_fingerprint: str
    workspace_id: str
    base_revision: str
    route: str
    state: str
    run_id: str | None
    attempt_id: str | None
    kernel_run_id: int | None
    branch: str | None
    worktree: str | None
    cancel_requested: bool
    detail: str | None
    evidence: dict | None
    created_at: float
    updated_at: float

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@dataclass(frozen=True)
class Approval:
    approval_id: str
    task_id: str
    task_revision: str
    spec_hash: str
    policy_fingerprint: str
    operation: str
    run_id: str | None
    allowed_actors: tuple[str, ...]
    state: str
    expires_at: float
    created_at: float
    decided_at: float | None
    decided_by: str | None


def _reservation_from_row(row: sqlite3.Row) -> Reservation:
    return Reservation(
        dispatch_id=row["dispatch_id"],
        task_id=row["task_id"],
        task_revision=row["task_revision"],
        spec_hash=row["spec_hash"],
        policy_fingerprint=row["policy_fingerprint"],
        workspace_id=row["workspace_id"],
        base_revision=row["base_revision"],
        route=row["route"],
        state=row["state"],
        run_id=row["run_id"],
        attempt_id=row["attempt_id"],
        kernel_run_id=row["kernel_run_id"],
        branch=row["branch"],
        worktree=row["worktree"],
        cancel_requested=bool(row["cancel_requested"]),
        detail=row["detail"],
        evidence=(json.loads(row["evidence"]) if row["evidence"] else None),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _approval_from_row(row: sqlite3.Row) -> Approval:
    return Approval(
        approval_id=row["approval_id"],
        task_id=row["task_id"],
        task_revision=row["task_revision"],
        spec_hash=row["spec_hash"],
        policy_fingerprint=row["policy_fingerprint"],
        operation=row["operation"],
        run_id=row["run_id"],
        allowed_actors=tuple(json.loads(row["allowed_actors"])),
        state=row["state"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
    )


class DispatchStore:
    """SQLite-backed receipt ledger. One process writes at a time; the
    kernel dispatch lock (held by the caller) provides that guarantee."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # The dispatcher and control commands may run in separate processes
        # (or threads) against the same sidecar — wait on a busy writer
        # instead of erroring on the first contention.
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript(_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):
                    raise
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "DispatchStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- reservations ---------------------------------------------------------

    def reserve(
        self,
        *,
        task_id: str,
        task_revision: str,
        spec_hash: str,
        policy_fingerprint: str,
        workspace_id: str,
        base_revision: str,
        route: str,
        consume_approval: str | None = None,
    ) -> Reservation:
        """Persist the reservation BEFORE any claim/HTTP/worktree effect.

        When ``consume_approval`` is given, the matching *applied* grant is
        burned to ``consumed`` in the SAME transaction as the reservation
        insert — and only when it still binds this exact scope (task
        revision + spec hash + policy fingerprint, unexpired). Any mismatch
        raises ``GrantMismatch`` and creates nothing.
        """
        now = time.time()
        dispatch_id = "d_" + secrets.token_hex(8)
        try:
            with self._conn:  # one txn: grant burn + insert are atomic
                if consume_approval is not None:
                    cur = self._conn.execute(
                        """
                        UPDATE approvals
                           SET state='consumed', decided_at=?,
                               decided_by=COALESCE(decided_by, 'jev-dispatch')
                         WHERE approval_id=? AND state='applied'
                           AND task_id=? AND task_revision=? AND spec_hash=?
                           AND policy_fingerprint=? AND operation='dispatch'
                           AND expires_at > ?
                        """,
                        (
                            now, consume_approval, task_id, task_revision,
                            spec_hash, policy_fingerprint, now,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise GrantMismatch(
                            f"approval {consume_approval} does not bind this "
                            "exact task/spec/policy scope — refusing to "
                            "consume it"
                        )
                self._conn.execute(
                    """
                    INSERT INTO reservations (
                        dispatch_id, task_id, task_revision, spec_hash,
                        policy_fingerprint, workspace_id, base_revision, route,
                        state, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        dispatch_id, task_id, task_revision, spec_hash,
                        policy_fingerprint, workspace_id, base_revision, route,
                        RESERVED, now, now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ReservationExists(task_id, workspace_id) from exc
        return self.get(dispatch_id)

    def get(self, dispatch_id: str) -> Reservation | None:
        row = self._conn.execute(
            "SELECT * FROM reservations WHERE dispatch_id = ?", (dispatch_id,)
        ).fetchone()
        return _reservation_from_row(row) if row else None

    def for_task(self, task_id: str) -> list[Reservation]:
        rows = self._conn.execute(
            "SELECT * FROM reservations WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ).fetchall()
        return [_reservation_from_row(r) for r in rows]

    def live_for_task(self, task_id: str) -> Reservation | None:
        """The active-or-unknown reservation blocking re-dispatch, if any."""
        rows = self._conn.execute(
            "SELECT * FROM reservations WHERE task_id = ? AND state IN "
            "('reserved','claimed','submitted','completing','unknown')",
            (task_id,),
        ).fetchall()
        return _reservation_from_row(rows[0]) if rows else None

    def list_live(self) -> list[Reservation]:
        rows = self._conn.execute(
            "SELECT * FROM reservations WHERE state IN "
            "('reserved','claimed','submitted','completing','unknown') "
            "ORDER BY created_at"
        ).fetchall()
        return [_reservation_from_row(r) for r in rows]

    def transition(
        self,
        dispatch_id: str,
        state: str,
        *,
        detail: str | None = None,
        run_id: str | None = None,
        attempt_id: str | None = None,
        kernel_run_id: int | None = None,
        branch: str | None = None,
        worktree: str | None = None,
        require_no_cancel: bool = False,
    ) -> Reservation:
        """Move a reservation; terminal states are one-way doors.

        ``require_no_cancel`` folds the cancellation check into the same
        atomic UPDATE — the guard against a late handoff cannot be
        interleaved with a ``request_cancel`` write.
        """
        if state not in ALL_STATES:
            raise StoreError(f"unknown reservation state {state!r}")
        guard = " AND cancel_requested = 0" if require_no_cancel else ""
        cur = self._conn.execute(
            f"""
            UPDATE reservations
               SET state = ?, detail = COALESCE(?, detail),
                   run_id = COALESCE(?, run_id),
                   attempt_id = COALESCE(?, attempt_id),
                   kernel_run_id = COALESCE(?, kernel_run_id),
                   branch = COALESCE(?, branch),
                   worktree = COALESCE(?, worktree),
                   updated_at = ?
             WHERE dispatch_id = ?
               AND state NOT IN ('review','blocked','aborted','cancelled','failed')
               {guard}
            """,
            (
                state, detail, run_id, attempt_id, kernel_run_id, branch,
                worktree, time.time(), dispatch_id,
            ),
        )
        self._conn.commit()
        if cur.rowcount != 1:
            existing = self.get(dispatch_id)
            raise StoreError(
                f"cannot transition {dispatch_id}: "
                f"current state {existing.state if existing else 'missing'!r}"
            )
        return self.get(dispatch_id)

    def record_evidence(self, dispatch_id: str, evidence: dict) -> None:
        """Persist sanitized verification/diff evidence durably — survives
        any later state transition (blocked, review, unknown)."""
        cur = self._conn.execute(
            "UPDATE reservations SET evidence = ?, updated_at = ? "
            "WHERE dispatch_id = ?",
            (json.dumps(evidence, sort_keys=True), time.time(), dispatch_id),
        )
        self._conn.commit()
        if cur.rowcount != 1:
            raise StoreError(f"no reservation {dispatch_id!r}")

    def request_cancel(self, dispatch_id: str) -> Reservation:
        """Persist the cancel intent BEFORE touching the wrapper — a crash
        after the HTTP call still finds the flag set."""
        cur = self._conn.execute(
            "UPDATE reservations SET cancel_requested = 1, updated_at = ? "
            "WHERE dispatch_id = ?",
            (time.time(), dispatch_id),
        )
        self._conn.commit()
        if cur.rowcount != 1:
            raise StoreError(f"no reservation {dispatch_id!r}")
        return self.get(dispatch_id)

    # -- approvals --------------------------------------------------------------

    def create_approval(
        self,
        *,
        task_id: str,
        task_revision: str,
        spec_hash: str,
        policy_fingerprint: str,
        operation: str,
        run_id: str | None,
        allowed_actors: list[str],
        ttl_seconds: int,
    ) -> Approval:
        """Durable approval bound to the exact dispatch scope: card revision,
        spec hash, policy fingerprint, operation, expiry. Pending until a
        configured operator acts. A pending grant for the identical scope is
        returned as-is — gating never piles up duplicate rows."""
        if not allowed_actors:
            raise StoreError(
                "approval requires a non-empty actor allowlist — nobody could "
                "ever apply it otherwise"
            )
        existing = self.pending_approval_for(
            task_id=task_id, operation=operation, task_revision=task_revision,
            spec_hash=spec_hash, policy_fingerprint=policy_fingerprint,
        )
        if existing is not None:
            return existing
        now = time.time()
        approval_id = "a_" + secrets.token_hex(8)
        self._conn.execute(
            """
            INSERT INTO approvals (
                approval_id, task_id, task_revision, spec_hash,
                policy_fingerprint, operation, run_id,
                allowed_actors, state, expires_at, created_at
            ) VALUES (?,?,?,?,?,?,?,?, 'pending', ?,?)
            """,
            (
                approval_id, task_id, task_revision, spec_hash,
                policy_fingerprint, operation, run_id,
                json.dumps(sorted(set(allowed_actors))), now + ttl_seconds, now,
            ),
        )
        self._conn.commit()
        return self.get_approval(approval_id)

    def get_approval(self, approval_id: str) -> Approval | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        return _approval_from_row(row) if row else None

    def pending_approval_for(
        self,
        task_id: str,
        operation: str,
        task_revision: str | None = None,
        spec_hash: str | None = None,
        policy_fingerprint: str | None = None,
    ) -> Approval | None:
        clauses = ["task_id = ?", "operation = ?", "state = 'pending'"]
        args: list = [task_id, operation]
        for col, val in (
            ("task_revision", task_revision),
            ("spec_hash", spec_hash),
            ("policy_fingerprint", policy_fingerprint),
        ):
            if val is not None:
                clauses.append(f"{col} = ?")
                args.append(val)
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE " + " AND ".join(clauses)
            + " ORDER BY created_at DESC LIMIT 1",
            args,
        ).fetchone()
        return _approval_from_row(row) if row else None

    def find_applied_grant(
        self,
        *,
        task_id: str,
        operation: str,
        task_revision: str,
        spec_hash: str,
        policy_fingerprint: str,
    ) -> Approval | None:
        """An applied (unburned) grant binding this exact scope — or None.
        Expired applied grants never match; they are marked expired."""
        now = time.time()
        self._conn.execute(
            "UPDATE approvals SET state='expired' WHERE state='applied' "
            "AND expires_at <= ?",
            (now,),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE task_id=? AND operation=? "
            "AND state='applied' AND task_revision=? AND spec_hash=? "
            "AND policy_fingerprint=? ORDER BY created_at DESC LIMIT 1",
            (task_id, operation, task_revision, spec_hash,
             policy_fingerprint),
        ).fetchone()
        return _approval_from_row(row) if row else None

    def _decide_approval(
        self, approval_id: str, actor: str, new_state: str
    ) -> Approval:
        """Atomic consume-once transition. Expiry denies/holds: a stale
        approval is marked expired and cannot apply."""
        now = time.time()
        with self._conn:  # single txn: read-check + write are atomic
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f"no approval {approval_id!r}")
            if row["state"] != "pending":
                raise StoreError(
                    f"approval {approval_id} is already {row['state']} — "
                    "approvals apply exactly once"
                )
            allowed = json.loads(row["allowed_actors"])
            if actor not in allowed:
                raise StoreError(
                    f"actor {actor!r} is not on this approval's allowlist"
                )
            if now >= row["expires_at"]:
                self._conn.execute(
                    "UPDATE approvals SET state='expired', decided_at=?, "
                    "decided_by=? WHERE approval_id=?",
                    (now, actor, approval_id),
                )
                self._conn.commit()  # persist the expiry BEFORE raising
                raise StoreError(
                    f"approval {approval_id} expired at "
                    f"{row['expires_at']:.0f} — deny/hold by default"
                )
            cur = self._conn.execute(
                "UPDATE approvals SET state=?, decided_at=?, decided_by=? "
                "WHERE approval_id=? AND state='pending'",
                (new_state, now, actor, approval_id),
            )
            if cur.rowcount != 1:
                raise StoreError(f"approval {approval_id} was already decided")
        return self.get_approval(approval_id)

    def apply_approval(self, approval_id: str, actor: str) -> Approval:
        return self._decide_approval(approval_id, actor, APPROVAL_APPLIED)

    def deny_approval(self, approval_id: str, actor: str) -> Approval:
        return self._decide_approval(approval_id, actor, APPROVAL_DENIED)
