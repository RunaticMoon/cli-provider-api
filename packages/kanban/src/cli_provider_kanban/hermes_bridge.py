"""Hermes kernel bridge — runs under the *installed Hermes interpreter*.

This file is executed as a script by the Hermes venv python (see
``kernel.KernelBridge``). It must import ONLY stdlib + ``hermes_cli`` — the
``cli_provider_kanban`` package is not installed there.

Protocol: one JSON object per line on stdin, one JSON object per line on
stdout::

    {"op": "claim", "args": {"task_id": "t_..."}}
    -> {"ok": true, "result": {...}}
    -> {"ok": false, "error": "...", "error_type": "..."}

All board mutation goes through the installed ``hermes_cli.kanban_db*`` API —
no status is ever invented via raw SQL. The only direct UPDATE is the narrow
``max_retries`` configuration field (there is no public setter), which caps
stock-reclaim replay for cards we dispatch; it never touches status.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import time
import traceback

_TASK_FIELDS = (
    "id", "title", "body", "assignee", "status", "priority", "created_by",
    "created_at", "started_at", "completed_at", "workspace_kind",
    "workspace_path", "claim_lock", "claim_expires", "tenant", "branch_name",
    "project_id", "result", "consecutive_failures", "worker_pid",
    "last_failure_error", "max_runtime_seconds", "current_run_id",
    "max_retries", "block_kind", "block_recurrences", "completion_contract",
)


def _task_dict(task) -> dict | None:
    if task is None:
        return None
    return {f: getattr(task, f, None) for f in _TASK_FIELDS}


def _parents(conn, task_id: str) -> list[str]:
    """Read the parent-edge table (read only — statuses are never written)."""
    try:
        rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
            (task_id,),
        ).fetchall()
        return sorted(str(r[0]) for r in rows)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Mutation fencing — see docs/JEV_STATE_REPAIR.md
#
# ``connect()`` returns an autocommit (isolation_level=None) connection and
# ``write_txn`` refuses nesting: a serial sequence of Python calls on one
# connection is NOT a cross-process transaction. The narrow correct adapter
# is a connection proxy handed to the EXISTING kernel mutator: when the
# mutator's ``write_txn`` issues its real ``BEGIN IMMEDIATE`` the proxy first
# lets the lock be acquired, then re-validates the caller's read-only
# ownership predicates *under that lock* — a second connection cannot
# interleave between the check and the mutation. A failed predicate rolls
# the fresh transaction back and raises ``_FenceError`` before any write.
# Deferred BEGINs, top-level SAVEPOINTs and bare autocommit writes are
# refused outright so a changed kernel transaction shape fails closed
# instead of silently skipping the fence.
# ---------------------------------------------------------------------------


class _FenceError(Exception):
    """A mutation guard failed under the board write lock — refused."""


_WRITE_HEADS = frozenset({
    "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER",
    "VACUUM", "REINDEX", "ANALYZE", "ATTACH", "DETACH",
})
_BEGIN_LOCK_RE = re.compile(r"^\s*BEGIN\s+(IMMEDIATE|EXCLUSIVE)\b", re.I)
_BEGIN_RE = re.compile(r"^\s*BEGIN\b", re.I)
_DML_IN_WITH_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|REPLACE\s+INTO)\b",
    re.I,
)


class _GuardedCursor:
    """Cursor proxy applying the owning proxy's gate to every statement."""

    def __init__(self, cursor, proxy):
        self._cursor = cursor
        self._proxy = proxy

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def execute(self, sql, parameters=()):
        if self._proxy._gate(sql) == "begin":
            # A boundary statement via a cursor still runs the lock+guard
            # flow on the real connection.
            self._proxy.execute(sql, parameters)
            return self
        return self._cursor.execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        self._proxy._gate(sql)
        return self._cursor.executemany(sql, seq_of_parameters)


class _GuardedConn:
    """Connection proxy fencing kernel mutations under the real write lock.

    Every top-level ``BEGIN IMMEDIATE``/``BEGIN EXCLUSIVE`` the kernel issues
    is allowed to acquire the database write lock first; the registered
    guards then run read-only predicates on the real connection while that
    lock is held. A failing guard rolls back and raises ``_FenceError``
    before the mutator writes anything. Anything that could mutate outside
    such a guarded transaction (deferred BEGIN, top-level SAVEPOINT,
    autocommit DML/DDL) is refused, so the fence cannot be skipped by a
    kernel transaction-shape change.
    """

    def __init__(self, conn, guards):
        self.__dict__["_conn"] = conn
        self.__dict__["_guards"] = list(guards)

    def __getattr__(self, name):
        return getattr(self.__dict__["_conn"], name)

    def _gate(self, sql) -> str:
        """Classify ``sql``; raise ``_FenceError`` for unguardable writes."""
        text = sql.strip()
        head = text.split(None, 1)[0].upper() if text else ""
        conn = self.__dict__["_conn"]
        if _BEGIN_LOCK_RE.match(text):
            return "begin"
        if _BEGIN_RE.match(text):
            # BEGIN DEFERRED takes the write lock at the first write, not at
            # BEGIN — the guard could not run under the lock atomically.
            if not conn.in_transaction:
                raise _FenceError(
                    "deferred transaction cannot be fenced atomically")
            return "ok"  # inside a guarded txn; sqlite errors if nested
        if head in ("COMMIT", "END", "ROLLBACK", "RELEASE"):
            return "ok"
        if head == "SAVEPOINT":
            if not conn.in_transaction:
                raise _FenceError(
                    "savepoint outside a fenced transaction refused")
            return "ok"
        write = head in _WRITE_HEADS
        if head == "PRAGMA" and "=" in text:
            write = True
        if head == "WITH" and _DML_IN_WITH_RE.search(text):
            write = True
        if write and not conn.in_transaction:
            raise _FenceError("unfenced autocommit mutation refused")
        return "ok"

    def execute(self, sql, parameters=()):
        if self._gate(sql) == "begin":
            conn = self.__dict__["_conn"]
            cur = conn.execute(sql, parameters)  # acquires the write lock
            try:
                for guard in self.__dict__["_guards"]:
                    guard(conn)
            except BaseException:
                with contextlib.suppress(Exception):
                    conn.execute("ROLLBACK")
                raise
            return cur
        return self.__dict__["_conn"].execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        self._gate(sql)
        return self.__dict__["_conn"].executemany(sql, seq_of_parameters)

    def executescript(self, script):
        # executescript implicitly COMMITs first — unfenceable, fail closed.
        raise _FenceError("executescript is not fenceable — refused")

    def cursor(self, *args, **kwargs):
        return _GuardedCursor(self.__dict__["_conn"].cursor(*args, **kwargs),
                              self)


def _task_row(conn, task_id: str):
    return conn.execute(
        "SELECT status, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def _latest_run_id(conn, task_id: str):
    return conn.execute(
        "SELECT MAX(id) FROM task_runs WHERE task_id = ?", (task_id,),
    ).fetchone()[0]


def _latest_block_event(conn, task_id: str):
    return conn.execute(
        "SELECT payload, run_id FROM task_events WHERE task_id = ? AND kind IN "
        "('blocked','block_loop_detected','dependency_wait') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def _block_reason(payload) -> str | None:
    try:
        data = json.loads(payload) if payload else None
    except ValueError:
        return None
    return data.get("reason") if isinstance(data, dict) else None


def _dispatch_block_marked(reason, dispatch_id: str) -> bool:
    """Anchored dispatch attribution on a block reason — the marker must sit
    at a fixed boundary (``resolve <id>:`` prefix or ``[dispatch <id>]``
    suffix); a floating substring is not attribution."""
    if not isinstance(reason, str) or not reason:
        return False
    return (
        reason.startswith(f"resolve {dispatch_id}:")
        or reason.endswith(f"[dispatch {dispatch_id}]")
    )


def _guard_blockable(task_id: str, ours):
    """Under the write lock: still blockable, and no foreign run is live."""
    def check(c):
        row = _task_row(c, task_id)
        if row is None:
            raise _FenceError(f"task {task_id} not found")
        status, cur = row[0], row[1]
        if status not in ("running", "ready"):
            raise _FenceError(
                f"task {task_id} left running/ready (status={status})")
        if cur is not None and (ours is None or int(cur) != int(ours)):
            raise _FenceError(
                f"task {task_id} has a live run owned by another actor")
    return check


def _guard_review_owned(task_id: str, expected):
    """Under the write lock: still in review, latest review handoff is ours."""
    def check(c):
        row = _task_row(c, task_id)
        if row is None or row[0] != "review":
            raise _FenceError(f"task {task_id} is no longer in review")
        cur = row[1]
        if cur is not None and int(cur) != int(expected):
            raise _FenceError(
                f"task {task_id} has a live run owned by another actor")
        ev = c.execute(
            "SELECT run_id FROM task_events WHERE task_id = ? "
            "AND kind = 'review_requested' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if ev is None or ev[0] is None or int(ev[0]) != int(expected):
            raise _FenceError(
                "the latest review handoff belongs to a different run")
    return check


def _guard_unblock_owned(task_id: str, *, ours, latest, reason_prefix,
                         reason_suffix):
    """Under the write lock: still blocked, the block is still the one the
    caller validated (run + anchored marker), and no newer run appeared."""
    def check(c):
        row = _task_row(c, task_id)
        if row is None:
            raise _FenceError(f"task {task_id} not found")
        status, cur = row[0], row[1]
        if status not in ("blocked", "scheduled"):
            raise _FenceError(
                f"task {task_id} is no longer blocked (status={status})")
        if cur is not None and (ours is None or int(cur) != int(ours)):
            raise _FenceError(
                f"task {task_id} carries a run owned by another actor")
        if latest is not None and _latest_run_id(c, task_id) != int(latest):
            raise _FenceError("a newer kernel run owns the card")
        if ours is not None or reason_prefix or reason_suffix:
            ev = _latest_block_event(c, task_id)
            if ev is None:
                raise _FenceError("no block event to attribute")
            if ours is not None and (ev[1] is None or int(ev[1]) != int(ours)):
                raise _FenceError("the current block is not this run's")
            reason = _block_reason(ev[0]) or ""
            if not (
                (reason_prefix and reason.startswith(reason_prefix))
                or (reason_suffix and reason.endswith(reason_suffix))
            ):
                raise _FenceError("the current block is not the caller's")
    return check


def _guard_sole_run(task_id: str, expected):
    """Under the write lock: no run newer than ``expected`` exists and no
    foreign run is live — a claim cannot ride over an interleaved run."""
    def check(c):
        row = _task_row(c, task_id)
        cur = row[1] if row else None
        if cur is not None and int(cur) != int(expected):
            raise _FenceError(
                f"task {task_id} has a live run owned by another actor")
        latest = _latest_run_id(c, task_id)
        if latest is None or int(latest) != int(expected):
            raise _FenceError("a newer kernel run used the card")
    return check


class _Bridge:
    def __init__(self, db_path: str):
        from pathlib import Path

        from hermes_cli.kanban_db_connect import connect

        self.db_path = db_path
        self.conn = connect(Path(db_path))
        self._tick_lock = None
        self._tick_held = False

    # -- lifecycle ------------------------------------------------------------

    def op_ping(self, args=None):
        return {"db": self.db_path, "pid": __import__("os").getpid()}

    def op_acquire_lock(self, args=None):
        """Kernel-held singleton dispatch lock (non-blocking flock)."""
        from hermes_cli.kanban_db_connect import _dispatch_tick_lock
        from pathlib import Path

        if self._tick_held:
            return {"held": True}
        self._tick_lock = _dispatch_tick_lock(Path(self.db_path))
        self._tick_held = bool(self._tick_lock.__enter__())
        if not self._tick_held:
            self._tick_lock = None
        return {"held": self._tick_held}

    def op_release_lock(self, args=None):
        if self._tick_lock is not None:
            self._tick_lock.__exit__(None, None, None)
            self._tick_lock = None
            self._tick_held = False
        return {"released": True}

    # -- tasks ------------------------------------------------------------------

    def op_create_task(self, args):
        from hermes_cli.kanban_db import create_task

        task_id = create_task(
            self.conn,
            title=args["title"],
            body=args.get("body"),
            assignee=args.get("assignee"),
            created_by=args.get("created_by", "jev-dispatch"),
            workspace_kind=args.get("workspace_kind", "scratch"),
            priority=int(args.get("priority", 0)),
            parents=tuple(args.get("parents") or ()),
            max_retries=args.get("max_retries", 1),
            # "running" means the normal flow: ready when parents are done,
            # todo otherwise. "blocked" parks the card for human ops.
            initial_status=args.get("initial_status", "running"),
            idempotency_key=args.get("idempotency_key"),
        )
        return {"task_id": task_id}

    def op_get_task(self, args):
        from hermes_cli.kanban_db import get_task

        task = get_task(self.conn, args["task_id"])
        data = _task_dict(task)
        if data is not None:
            tid = args["task_id"]
            data["parents"] = _parents(self.conn, tid)
            row = self.conn.execute(
                "SELECT metadata FROM task_runs WHERE task_id = ? "
                "ORDER BY id DESC LIMIT 1", (tid,),
            ).fetchone()
            data["last_run_metadata"] = row[0] if row else None
            # Run-ownership provenance (read only) so callers can fence every
            # mutation to the run that actually produced the card's state.
            data["latest_run_id"] = self.conn.execute(
                "SELECT MAX(id) FROM task_runs WHERE task_id = ?", (tid,),
            ).fetchone()[0]
            ev = self.conn.execute(
                "SELECT run_id FROM task_events WHERE task_id = ? "
                "AND kind = 'review_requested' ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            data["review_run_id"] = ev[0] if ev else None
            bl = self.conn.execute(
                "SELECT kind, payload, run_id FROM task_events "
                "WHERE task_id = ? AND kind IN "
                "('blocked','block_loop_detected','dependency_wait') "
                "ORDER BY id DESC LIMIT 1", (tid,),
            ).fetchone()
            data["last_block"] = None
            if bl is not None:
                reason = None
                try:
                    payload = json.loads(bl[1]) if bl[1] else None
                    if isinstance(payload, dict):
                        reason = payload.get("reason")
                except ValueError:
                    reason = None
                data["last_block"] = {
                    "kind": bl[0], "reason": reason, "run_id": bl[2],
                }
        return {"task": data}

    def op_set_max_retries(self, args):
        """Narrow configuration-field write (no public setter exists). Caps
        stock-reclaim replay at ``value``; never touches status."""
        value = int(args["value"])
        cur = self.conn.execute(
            "UPDATE tasks SET max_retries = ? WHERE id = ? "
            "AND (max_retries IS NULL OR max_retries > ?)",
            (value, args["task_id"], value),
        )
        self.conn.commit()
        return {"updated": cur.rowcount == 1}

    def op_claim(self, args):
        from hermes_cli.kanban_db import claim_task, get_task

        expected = args.get("expected_run_id")
        conn = self.conn
        if expected is not None:
            # Fenced claim: under the kernel's own BEGIN IMMEDIATE, prove no
            # run newer than the caller's exists and no foreign run is live
            # before the canonical claim runs — a second connection cannot
            # interleave between the resolve-time read and this mutation.
            conn = _GuardedConn(
                conn, [_guard_sole_run(args["task_id"], expected)])
        try:
            claimed = claim_task(
                conn,
                args["task_id"],
                ttl_seconds=args.get("ttl_seconds"),
                claimer=args.get("claimer"),
            )
        except _FenceError as exc:
            return {"claimed": False, "refused": True, "reason": str(exc)}
        data = _task_dict(claimed)
        if data is not None:
            # claim_task returns the row pre-run-open on some versions; re-read
            # so current_run_id is always populated for fencing.
            data = _task_dict(get_task(self.conn, args["task_id"]))
            data["parents"] = _parents(self.conn, args["task_id"])
        return {"claimed": claimed is not None, "task": data}

    def op_heartbeat(self, args):
        from hermes_cli.kanban_db import heartbeat_claim

        return {
            "alive": heartbeat_claim(
                self.conn,
                args["task_id"],
                ttl_seconds=args.get("ttl_seconds"),
                claimer=args.get("claimer"),
            )
        }

    def op_block(self, args):
        from hermes_cli.kanban_db import block_task

        return {
            "blocked": block_task(
                self.conn,
                args["task_id"],
                reason=args.get("reason"),
                kind=args.get("kind"),
                expected_run_id=args.get("expected_run_id"),
            )
        }

    def op_block_owned(self, args):
        """Block fenced to run ownership — the guarded form of ``block``.

        ``run_id`` is the kernel run the CALLER owns. The pre-read is only a
        hint: the read and the mutation are separate autocommit statements,
        so a foreign claim can interleave between them. The guarded
        connection re-validates "still running/ready and no foreign run is
        live" under the kernel mutator's own ``BEGIN IMMEDIATE`` write lock
        and rolls back before any write — a card claimed by another
        process after our read is refused untouched.
        """
        from hermes_cli.kanban_db import block_task, get_task

        task = get_task(self.conn, args["task_id"])
        if task is None:
            return {"blocked": False, "reason": "task not found"}
        if task.status not in ("running", "ready"):
            return {
                "blocked": False,
                "skipped": True,
                "status": task.status,
            }
        ours = args.get("run_id")
        cur = task.current_run_id
        if cur is not None:
            if ours is None or int(cur) != int(ours):
                return {
                    "blocked": False,
                    "reason": "card has a live run owned by another actor",
                    "current_run_id": int(cur),
                }
            expected = int(ours)
        else:
            expected = None
        conn = _GuardedConn(
            self.conn,
            [_guard_blockable(args["task_id"], ours)],
        )
        try:
            return {
                "blocked": block_task(
                    conn,
                    args["task_id"],
                    reason=args.get("reason"),
                    kind=args.get("kind"),
                    expected_run_id=expected,
                )
            }
        except _FenceError as exc:
            return {"blocked": False, "refused": True, "reason": str(exc)}

    def op_reopen_review_if(self, args):
        """Reopen a review ONLY when the handoff's run is the caller's own —
        a stale receipt must never demote a newer dispatch's review.

        The pre-read only filters obviously-stale calls: the reopened
        review's run id is re-validated under the mutator's own write lock,
        so a second connection that reopens/reclaims/requests a newer
        review between our read and the mutation wins and we refuse.
        """
        from hermes_cli.kanban_db import reopen_review_task

        expected = args.get("expected_run_id")
        row = self.conn.execute(
            "SELECT run_id FROM task_events WHERE task_id = ? "
            "AND kind = 'review_requested' ORDER BY id DESC LIMIT 1",
            (args["task_id"],),
        ).fetchone()
        review_run = row[0] if row else None
        if expected is None or review_run is None or int(review_run) != int(expected):
            return {
                "reopened": False,
                "reason": "review handoff belongs to a different run",
                "review_run_id": review_run,
            }
        conn = _GuardedConn(
            self.conn,
            [_guard_review_owned(args["task_id"], expected)],
        )
        try:
            return {"reopened": reopen_review_task(conn, args["task_id"])}
        except _FenceError as exc:
            return {
                "reopened": False,
                "refused": True,
                "reason": str(exc),
                "review_run_id": review_run,
            }

    def op_request_review(self, args):
        from hermes_cli.kanban_db import request_review

        ok, reason = request_review(
            self.conn,
            args["task_id"],
            summary=args.get("summary"),
            metadata=args.get("metadata"),
            reviewer=args.get("reviewer"),
            expected_run_id=args.get("expected_run_id"),
            with_reason=True,
        )
        return {"ok": ok, "reason": reason}

    def op_complete(self, args):
        """Lead accept only — the dispatcher never completes a card itself."""
        from hermes_cli.kanban_db import complete_task

        return {
            "done": complete_task(
                self.conn,
                args["task_id"],
                result=args.get("result"),
                summary=args.get("summary"),
                metadata=args.get("metadata"),
                expected_run_id=args.get("expected_run_id"),
                force=bool(args.get("force", False)),
            )
        }

    def op_unblock(self, args):
        """blocked/scheduled -> resumable phase (used after approval apply)."""
        from hermes_cli.kanban_db import unblock_task

        return {"unblocked": unblock_task(self.conn, args["task_id"])}

    def op_unblock_owned(self, args):
        """Unblock only the block the caller validated — fenced ``unblock``.

        The kernel ``unblock_task`` CASes on status alone, so a bare call
        can clear a *different* block placed after the caller's read — or
        reclaim a foreign run that claimed the card in between. Under the
        mutator's own write lock the guarded connection re-checks that the
        card is still blocked, carries no run newer than ``latest_run_id``,
        has no foreign run live (``run_id`` is the caller's own, or None
        for a pre-claim block), and that the latest block event is still
        the run and reason the caller attributed — matched on the anchored
        ``resolve <dispatch>:`` prefix / ``[dispatch <dispatch>]`` suffix
        marker, never a substring.
        """
        from hermes_cli.kanban_db import unblock_task

        tid = args["task_id"]
        conn = _GuardedConn(self.conn, [_guard_unblock_owned(
            tid,
            ours=args.get("run_id"),
            latest=args.get("latest_run_id"),
            reason_prefix=args.get("reason_prefix"),
            reason_suffix=args.get("reason_suffix"),
        )])
        try:
            return {"unblocked": unblock_task(conn, tid)}
        except _FenceError as exc:
            return {"unblocked": False, "refused": True, "reason": str(exc)}

    def op_reopen_review(self, args):
        """review -> resumable work state — the cancel/rollback path when a
        card reached review but must not stay promotable."""
        from hermes_cli.kanban_db import reopen_review_task

        return {"reopened": reopen_review_task(self.conn, args["task_id"])}

    def op_notify_sub(self, args):
        """Reuse Hermes notify subscriptions for the approval/review handoff."""
        from hermes_cli.kanban_db_notify import add_notify_sub

        add_notify_sub(
            self.conn,
            task_id=args["task_id"],
            platform=args["platform"],
            chat_id=args["chat_id"],
            thread_id=args.get("thread_id"),
            user_id=args.get("user_id"),
        )
        return {"subscribed": True}

    def op_list_scope(self, args):
        """In-scope tasks via the kernel ``list_tasks`` API (one call per
        status — the kernel filters a single status at a time)."""
        from hermes_cli.kanban_db import list_tasks

        limit = int(args.get("limit", 256))
        tasks = []
        for status in args["statuses"]:
            for task in list_tasks(
                self.conn, assignee=args["assignee"], status=status, limit=limit
            ):
                data = _task_dict(task)
                data["parents"] = _parents(self.conn, task.id)
                tasks.append(data)
        tasks.sort(key=lambda t: (-(t.get("priority") or 0), t.get("created_at") or 0))
        return {"tasks": tasks[:limit]}


def _dispatch(bridge, req):
    op = req.get("op")
    handler = getattr(_Bridge, "op_" + str(op), None)
    if handler is None or str(op).startswith("_"):
        return {"ok": False, "error": f"unknown op {op!r}", "error_type": "ValueError"}
    try:
        return {"ok": True, "result": handler(bridge, req.get("args") or {})}
    except Exception as exc:  # surfaced to the caller verbatim
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "trace": traceback.format_exc(limit=8),
        }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: hermes_bridge.py <kanban.db path>", file=sys.stderr)
        return 2
    try:
        bridge = _Bridge(sys.argv[1])
    except Exception as exc:
        json.dump({"ok": False, "error": str(exc), "error_type": type(exc).__name__}, sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 1
    json.dump({"ok": True, "result": {"ready": True, "time": time.time()}}, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            json.dump({"ok": False, "error": f"bad request json: {exc}", "error_type": "ValueError"}, sys.stdout)
            sys.stdout.write("\n")
            sys.stdout.flush()
            continue
        resp = _dispatch(bridge, req)
        json.dump(resp, sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()
        if req.get("op") == "shutdown":
            break
    return 0


def _shutdown(bridge):
    return {"bye": True}


_Bridge.op_shutdown = _shutdown

if __name__ == "__main__":
    raise SystemExit(main())
