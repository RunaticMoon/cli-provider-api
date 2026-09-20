"""Control operations — receipt-based, never chat-auth.

Every operation works off the durable dispatch receipt (sidecar) plus the
canonical stores: kanban for task state, the wrapper for run state.

- ``status``   reconcile receipt + wrapper run + kanban status
- ``cancel``   persist the intent FIRST, then cancel the wrapper run; a late
               completion can never be upgraded to review/done afterwards
- ``approve`` / ``deny``  atomic consume-once approval records, actor-checked
               against the policy's configured operator allowlist
- ``accept``   the ONLY path to ``done``: requires review proof + a recorded
               integrated revision + a fresh cancellation check
- ``resolve``  reconcile an ``unknown`` receipt against wrapper truth without
               ever re-executing

The ``--actor`` flag is matched against ``policy.control.operators`` — MVP
local-operator auth only. It is NOT Telegram authentication.
"""

from __future__ import annotations

import json
from pathlib import Path

from .kernel import KernelBridge
from .policy import Policy, load_policy
from .store import (
    CANCELLED,
    FAILED,
    REVIEW,
    UNKNOWN,
    DispatchStore,
    StoreError,
)
from .worktree import validate_commit, WorktreeError
from .wrapper_client import (
    WrapperClient,
    WrapperError,
    WrapperHTTPError,
    WrapperTransportError,
)


class ControlError(Exception):
    """Control precondition/authorization failure."""


def _client_for(policy: Policy) -> WrapperClient:
    if policy.execution is None:
        raise ControlError("policy has no execution target configured")
    return WrapperClient(
        policy.execution.base_url,
        credential_file=policy.execution.credential_file,
        timeout_seconds=policy.dispatch.http_timeout_seconds,
    )


def _require_operator(policy: Policy, actor: str) -> None:
    if actor not in policy.control.operators:
        raise ControlError(
            f"actor {actor!r} is not a configured operator — "
            "a bare --actor string is not authentication"
        )


def cmd_status(
    *,
    store_path: str | Path,
    board_db: str | Path,
    policy_path: str | Path,
    dispatch_id: str | None = None,
    task_id: str | None = None,
) -> dict:
    """Reconcile one receipt against wrapper + kanban truth."""
    policy = load_policy(policy_path)
    store = DispatchStore(store_path)
    try:
        res = None
        if dispatch_id:
            res = store.get(dispatch_id)
        elif task_id:
            live = store.live_for_task(task_id)
            hist = store.for_task(task_id)
            res = live or (hist[-1] if hist else None)
        if res is None:
            raise ControlError("no matching dispatch receipt")

        out = {
            "receipt": {
                "dispatch_id": res.dispatch_id,
                "task_id": res.task_id,
                "state": res.state,
                "route": res.route,
                "run_id": res.run_id,
                "attempt_id": res.attempt_id,
                "cancel_requested": res.cancel_requested,
                "detail": res.detail,
            },
            "run": None,
            "task_status": None,
        }
        if res.run_id:
            try:
                out["run"] = _client_for(policy).get_run(res.run_id)
            except WrapperError as exc:
                out["run_error"] = str(exc)
        with KernelBridge(board_db, cfg=policy.hermes) as kernel:
            task = kernel.call("get_task", task_id=res.task_id).get("task")
            if task:
                out["task_status"] = task["status"]
        return out
    finally:
        store.close()


def cmd_cancel(
    *,
    store_path: str | Path,
    board_db: str | Path,
    policy_path: str | Path,
    dispatch_id: str,
    actor: str,
) -> dict:
    """Persist the cancel intent first, then cancel the wrapper run.

    The dispatcher checks ``cancel_requested`` before any review handoff, so
    a completion racing this call can never be upgraded to review/done.
    """
    policy = load_policy(policy_path)
    _require_operator(policy, actor)
    store = DispatchStore(store_path)
    try:
        res = store.get(dispatch_id)
        if res is None:
            raise ControlError(f"no dispatch receipt {dispatch_id!r}")
        res = store.request_cancel(dispatch_id)
        result = {"dispatch_id": dispatch_id, "cancel_requested": True,
                  "wrapper": None, "task": None}
        if res.run_id:
            try:
                result["wrapper"] = _client_for(policy).cancel_run(res.run_id)
            except WrapperHTTPError as exc:
                if exc.status != 404:
                    raise
                result["wrapper"] = {"run_id": res.run_id, "status": "not_found"}
        with KernelBridge(board_db, cfg=policy.hermes) as kernel:
            task = kernel.call("get_task", task_id=res.task_id).get("task")
            if task and task["status"] in ("running", "ready"):
                kernel.call(
                    "block", task_id=res.task_id, kind="needs_input",
                    expected_run_id=res.kernel_run_id,
                    reason=f"cancelled by operator {actor} "
                           f"(dispatch {dispatch_id})",
                )
                task = kernel.call("get_task", task_id=res.task_id).get("task")
            result["task"] = task["status"] if task else None
        if not res.is_terminal:
            store.transition(dispatch_id, CANCELLED,
                             detail=f"cancelled by {actor}")
        return result
    finally:
        store.close()


def cmd_approve(
    *,
    store_path: str | Path,
    board_db: str | Path,
    policy_path: str | Path,
    approval_id: str,
    actor: str,
) -> dict:
    """Apply a pending approval exactly once, then unblock the card so the
    next tick re-dispatches it (the old receipt is already terminal)."""
    policy = load_policy(policy_path)
    _require_operator(policy, actor)
    store = DispatchStore(store_path)
    try:
        try:
            approval = store.apply_approval(approval_id, actor)
        except StoreError as exc:
            raise ControlError(str(exc)) from exc
        with KernelBridge(board_db, cfg=policy.hermes) as kernel:
            kernel.call("unblock", task_id=approval.task_id)
            task = kernel.call("get_task", task_id=approval.task_id).get("task")
        return {
            "approval_id": approval_id,
            "state": approval.state,
            "task_id": approval.task_id,
            "task_status": task["status"] if task else None,
        }
    finally:
        store.close()


def cmd_deny(
    *,
    store_path: str | Path,
    approval_id: str,
    actor: str,
    policy_path: str | Path,
) -> dict:
    policy = load_policy(policy_path)
    _require_operator(policy, actor)
    store = DispatchStore(store_path)
    try:
        try:
            approval = store.deny_approval(approval_id, actor)
        except StoreError as exc:
            raise ControlError(str(exc)) from exc
        return {"approval_id": approval_id, "state": approval.state}
    finally:
        store.close()


def cmd_accept(
    *,
    store_path: str | Path,
    board_db: str | Path,
    policy_path: str | Path,
    task_id: str,
    actor: str,
    integrated_revision: str,
) -> dict:
    """The ONLY path to ``done`` — requires all of:

    - the card is in kanban ``review`` (review proof),
    - our receipt reached ``review`` state,
    - a full-commit integrated revision,
    - no pending cancellation on the receipt and the wrapper run (if known)
      is not cancelled.
    """
    policy = load_policy(policy_path)
    _require_operator(policy, actor)
    try:
        validate_commit(integrated_revision)
    except WorktreeError as exc:
        raise ControlError(str(exc)) from exc

    store = DispatchStore(store_path)
    try:
        receipts = store.for_task(task_id)
        receipt = next((r for r in receipts if r.state == REVIEW), None)
        if receipt is None:
            raise ControlError(
                f"no review-state dispatch receipt for {task_id} — a card "
                "must pass through dispatch->review before accept"
            )
        if receipt.cancel_requested:
            raise ControlError(
                f"dispatch {receipt.dispatch_id} has a cancellation request — "
                "a cancelled run cannot be accepted"
            )
        if receipt.run_id:
            run = _client_for(policy).get_run(receipt.run_id)
            if run and run.get("status") == "cancelled":
                raise ControlError(
                    f"wrapper run {receipt.run_id} is cancelled — refusing "
                    "accept (latest cancellation check)"
                )
        with KernelBridge(board_db, cfg=policy.hermes) as kernel:
            task = kernel.call("get_task", task_id=task_id).get("task")
            if not task or task["status"] != "review":
                raise ControlError(
                    f"card {task_id} is not in review "
                    f"(status={task['status'] if task else 'missing'})"
                )
            done = kernel.call(
                "complete", task_id=task_id,
                result=json.dumps({
                    "integrated_revision": integrated_revision,
                    "accepted_by": actor,
                    "dispatch_id": receipt.dispatch_id,
                }),
                summary=f"accepted by {actor}; integrated {integrated_revision[:12]}",
                metadata={"integrated_revision": integrated_revision,
                          "dispatch_id": receipt.dispatch_id},
            )
            if not done["done"]:
                raise ControlError(
                    f"kernel refused completion for {task_id}"
                )
            task = kernel.call("get_task", task_id=task_id).get("task")
        return {
            "task_id": task_id,
            "task_status": task["status"],
            "integrated_revision": integrated_revision,
            "dispatch_id": receipt.dispatch_id,
        }
    finally:
        store.close()


def cmd_resolve(
    *,
    store_path: str | Path,
    board_db: str | Path,
    policy_path: str | Path,
    dispatch_id: str,
    actor: str,
) -> dict:
    """Reconcile an ``unknown`` receipt against wrapper truth — never replays.

    completed run -> the work exists: the card is moved to review via the
    kernel fence and the receipt closed ``review``. failed/cancelled -> the
    receipt mirrors the terminal state and the card stays blocked for rework.
    still running -> report only.
    """
    policy = load_policy(policy_path)
    _require_operator(policy, actor)
    store = DispatchStore(store_path)
    try:
        res = store.get(dispatch_id)
        if res is None or res.state != UNKNOWN:
            raise ControlError(
                f"resolve requires an unknown-state receipt, got "
                f"{res.state if res else 'missing'}"
            )
        run = _client_for(policy).get_run(res.run_id) if res.run_id else None
        if run is None:
            # Transport truly never landed: nothing executed. Abort the
            # receipt so the card can be dispatched again — the ONLY case
            # where unknown resolves to retryable.
            store.transition(dispatch_id, "aborted",
                             detail=f"no wrapper run exists; nothing executed "
                                    f"(resolved by {actor})")
            return {"dispatch_id": dispatch_id, "resolved": "aborted",
                    "reason": "no wrapper run found"}
        status = str(run.get("status") or "unknown")
        if status == "completed":
            with KernelBridge(board_db, cfg=policy.hermes) as kernel:
                kernel.call("unblock", task_id=res.task_id)
                resp = kernel.call(
                    "request_review", task_id=res.task_id,
                    summary=f"resolved unknown dispatch {dispatch_id}: run "
                            f"{res.run_id} completed",
                    metadata={"dispatch_id": dispatch_id,
                              "wrapper_run_id": res.run_id},
                    expected_run_id=res.kernel_run_id,
                )
                if not resp["ok"]:
                    raise ControlError(
                        f"review handoff refused during resolve: "
                        f"{resp.get('reason')}"
                    )
            store.transition(dispatch_id, REVIEW,
                             detail=f"resolved by {actor}: run completed")
            return {"dispatch_id": dispatch_id, "resolved": "review",
                    "run_status": status}
        if status in ("failed", "cancelled"):
            store.transition(
                dispatch_id, CANCELLED if status == "cancelled" else FAILED,
                detail=f"resolved by {actor}: run {status}")
            return {"dispatch_id": dispatch_id, "resolved": status,
                    "run_status": status}
        return {"dispatch_id": dispatch_id, "resolved": "none",
                "run_status": status}
    finally:
        store.close()
