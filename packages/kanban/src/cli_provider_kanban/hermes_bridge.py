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

import json
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
            data["parents"] = _parents(self.conn, args["task_id"])
            row = self.conn.execute(
                "SELECT metadata FROM task_runs WHERE task_id = ? "
                "ORDER BY id DESC LIMIT 1", (args["task_id"],),
            ).fetchone()
            data["last_run_metadata"] = row[0] if row else None
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

        claimed = claim_task(
            self.conn,
            args["task_id"],
            ttl_seconds=args.get("ttl_seconds"),
            claimer=args.get("claimer"),
        )
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
