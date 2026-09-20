"""Control operations — receipt-based, never chat-auth.

Every operation works off the durable dispatch receipt (sidecar) plus the
canonical stores: kanban for task state, the wrapper for run state.

- ``status``   reconcile receipt + wrapper run + kanban status
- ``cancel``   persist the intent FIRST, then cancel the wrapper run; a late
               completion can never be upgraded to review/done afterwards.
               A wrapper 404 or transport failure is reported as NOT
               confirmed — the durable local intent is what stops handoff.
- ``approve`` / ``deny``  atomic consume-once approval records, actor-bound
               to the current OS user AND the configured operator list
- ``accept``   DISABLED — the Lead verifies review state and completes via
               Hermes itself; this slice has no safe proof contract
- ``resolve``  reconcile an ``unknown`` receipt against wrapper truth
               without ever re-executing; absence of a run is NOT proof of
               absence — it stays unknown pending external investigation

``--actor`` alone is never authentication: the actor must equal
``pwd.getpwuid(os.geteuid())`` (LOCAL CLI auth — not Telegram), or map to
the current euid via ``control.operator_uids`` when configured.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

from .evidence import persist_diff
from .kernel import KernelBridge, KernelError
from .policy import Policy, load_policy, policy_fingerprint
from .spec import resolve_spec
from .store import (
    ABORTED,
    BLOCKED,
    CANCELLED,
    FAILED,
    REVIEW,
    UNKNOWN,
    DispatchStore,
    StoreError,
)
from .worktree import (
    Worktree,
    WorktreeError,
    _git_dir_of,
    capture_diff,
    changed_files,
    check_scope,
    collect_artifacts,
    resolve_repo,
    run_verification,
    sanitize_text,
    sha256_file,
)
from .wrapper_client import (
    WrapperClient,
    WrapperError,
    WrapperHTTPError,
)


class ControlError(Exception):
    """Control precondition/authorization failure."""


def _control_client(policy: Policy) -> WrapperClient:
    """Run-control calls (get/cancel/artifact) go to the CONTROL target —
    ``execution.control_base_url`` — which direct mode defaults to the data
    base URL. The data POST URL is never assumed to serve run truth. The
    ``execution.allow_installed_gateway`` opt-in deliberately does NOT
    propagate here: the control plane keeps refusing the installed
    service port — the policy already binds it to a distinct loopback
    wrapper base instead."""
    if policy.execution is None:
        raise ControlError("policy has no execution target configured")
    exe = policy.execution
    return WrapperClient(
        exe.control_base_url or exe.base_url,
        credential_file=exe.control_credential_file or exe.credential_file,
        timeout_seconds=policy.dispatch.http_timeout_seconds,
    )


def _dispatch_block_marked(reason, dispatch_id: str) -> bool:
    """Anchored dispatch attribution on a block reason — the marker must sit
    at a fixed boundary (``resolve <id>:`` prefix or ``[dispatch <id>]``
    suffix); a floating substring is not attribution. Mirrors the bridge's
    rule for the ``unblock_owned`` fence."""
    if not isinstance(reason, str) or not reason:
        return False
    return (
        reason.startswith(f"resolve {dispatch_id}:")
        or reason.endswith(f"[dispatch {dispatch_id}]")
    )


def _require_operator(policy: Policy, actor: str) -> None:
    """The actor must be a configured operator AND the current OS user —
    a bare string on the CLI is not authentication. When the policy carries
    an explicit ``operator_uids`` map it is authoritative instead."""
    if actor not in policy.control.operators:
        raise ControlError(
            f"actor {actor!r} is not a configured operator — "
            "a bare --actor string is not authentication"
        )
    euid = os.geteuid()
    if policy.control.operator_uids is not None:
        if policy.control.operator_uids.get(actor) != euid:
            raise ControlError(
                f"actor {actor!r} does not map to the current OS identity "
                f"(euid {euid}) under control.operator_uids — denied"
            )
        return
    os_user = pwd.getpwuid(euid).pw_name
    if actor != os_user:
        raise ControlError(
            f"actor {actor!r} does not match the current OS identity "
            f"{os_user!r} — --actor must be the invoking local user "
            "(or an explicit control.operator_uids mapping)"
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
                out["run"] = _control_client(policy).get_run(res.run_id)
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

    The dispatcher checks ``cancel_requested`` atomically before submit and
    before the review handoff, so a completion racing this call can never be
    upgraded to review/done. The wrapper response is reported truthfully:
    ``confirmed`` only when the wrapper actually confirms; a 404 is
    ``not_found``, a transport failure is ``unreachable`` — never claimed
    as a confirmed cancellation.
    """
    policy = load_policy(policy_path)
    _require_operator(policy, actor)
    store = DispatchStore(store_path)
    try:
        res = store.get(dispatch_id)
        if res is None:
            raise ControlError(f"no dispatch receipt {dispatch_id!r}")
        if res.state in (ABORTED, BLOCKED, FAILED, CANCELLED):
            # A terminal, never-live receipt cancels nothing: it must never
            # mutate a board that a NEWER dispatch may own, and it must
            # never signal a run it does not have.
            raise ControlError(
                f"receipt {dispatch_id} is already terminal "
                f"({res.state}) — nothing left to cancel"
            )
        res = store.request_cancel(dispatch_id)
        result = {"dispatch_id": dispatch_id, "cancel_requested": True,
                  "wrapper": None, "task": None}
        if res.run_id:
            try:
                wrapper = _control_client(policy).cancel_run(res.run_id)
                wrapper.setdefault("confirmed", False)
                result["wrapper"] = wrapper
            except WrapperHTTPError as exc:
                if exc.status == 404:
                    result["wrapper"] = {
                        "run_id": res.run_id,
                        "status": "not_found",
                        "confirmed": False,
                    }
                else:
                    result["wrapper"] = {
                        "run_id": res.run_id,
                        "status": "error",
                        "confirmed": False,
                        "detail": str(exc),
                    }
            except WrapperError as exc:
                result["wrapper"] = {
                    "run_id": res.run_id,
                    "status": "unreachable",
                    "confirmed": False,
                    "detail": str(exc),
                }
        with KernelBridge(board_db, cfg=policy.hermes) as kernel:
            task = kernel.call("get_task", task_id=res.task_id).get("task")
            if task and task["status"] in ("running", "ready"):
                blk = kernel.call(
                    "block_owned", task_id=res.task_id, kind="needs_input",
                    run_id=res.kernel_run_id,
                    reason=f"cancelled by operator {actor} "
                           f"(dispatch {dispatch_id})",
                )
                if not blk.get("blocked"):
                    result["board"] = {"block": blk}
                task = kernel.call("get_task", task_id=res.task_id).get("task")
            elif task and task["status"] == "review":
                # A cancel after the review handoff must not leave a
                # cancelled execution looking promotable — but ONLY when
                # THIS receipt's run produced that handoff. A review
                # belonging to a different (newer) run is left untouched.
                ro = kernel.call(
                    "reopen_review_if", task_id=res.task_id,
                    expected_run_id=res.kernel_run_id,
                )
                if ro.get("reopened"):
                    kernel.call(
                        "block_owned", task_id=res.task_id, kind="needs_input",
                        run_id=res.kernel_run_id,
                        reason=f"cancelled by operator {actor} after review "
                               f"handoff (dispatch {dispatch_id}) — unblock "
                               "explicitly to re-dispatch",
                    )
                else:
                    result["board"] = {"reopen_review": ro}
                task = kernel.call("get_task", task_id=res.task_id).get("task")
            result["task"] = task["status"] if task else None
        try:
            store.transition(dispatch_id, CANCELLED,
                             detail=f"cancelled by {actor}")
        except StoreError:
            # The receipt closed between request_cancel and now — the intent
            # is durable either way; report truthfully what it landed on.
            result["receipt_state"] = store.get(dispatch_id).state
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
    next tick consumes the grant atomically with its reservation — for the
    exact task-revision/spec-hash/policy-fingerprint scope only."""
    policy = load_policy(policy_path)
    _require_operator(policy, actor)
    store = DispatchStore(store_path)
    try:
        try:
            approval = store.apply_approval(approval_id, actor)
        except StoreError as exc:
            raise ControlError(str(exc)) from exc
        with KernelBridge(board_db, cfg=policy.hermes) as kernel:
            # Fenced: unblock only the block THIS approval filed — the
            # anchored ``needs_approval (<id>):`` marker must still be the
            # card's latest block reason under the write lock, so a later
            # foreign block (or a live run) is never silently cleared.
            ub = kernel.call(
                "unblock_owned", task_id=approval.task_id, run_id=None,
                reason_prefix=f"needs_approval ({approval.approval_id}):",
            )
            task = kernel.call("get_task", task_id=approval.task_id).get("task")
        return {
            "approval_id": approval_id,
            "state": approval.state,
            "task_id": approval.task_id,
            "task_status": task["status"] if task else None,
            "unblocked": bool(ub.get("unblocked")),
            **({"card_note": ub.get("reason")} if ub.get("reason") else {}),
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
    """DISABLED — fail closed.

    There is no safe proof contract in this slice: a review-state card plus
    a caller-supplied 40-hex string is not evidence of an integrated,
    verified revision. The Lead must inspect the review handoff, verify the
    integrated revision themselves, and complete the card through Hermes.
    """
    raise ControlError(
        "accept is disabled: review evidence does not prove an integrated "
        "revision — the Lead must verify the review state and complete the "
        "card via Hermes directly"
    )


def _resolve_spec_for(policy, policy_path, task_dict):
    """The card's spec from the same contract sources dispatch uses (card
    body or the operator task_map) — resolve runs the SAME verification."""
    from .board import BoardTask
    from .dispatch import _task_board_fields
    from .shadow import _load_task_map

    task_map = _load_task_map(policy, Path(policy_path))
    task = BoardTask(**_task_board_fields(task_dict))
    return resolve_spec(
        task, task_map,
        max_body_bytes=policy.limits.max_body_bytes,
        max_spec_bytes=policy.limits.max_spec_bytes,
    ).spec


def cmd_resolve(
    *,
    store_path: str | Path,
    board_db: str | Path,
    policy_path: str | Path,
    dispatch_id: str,
    actor: str,
) -> dict:
    """Reconcile an ``unknown`` receipt against wrapper truth — never replays.

    - no run id / 404 / unreachable -> stays ``unknown`` (absence is NOT
      proof nothing executed; an operator investigates externally)
    - cancelled / failed -> the receipt mirrors the terminal truth
    - completed -> requires outcome ``succeeded`` AND the intact bound
      worktree AND the full verification contract AND no pending cancel —
      the same bar as the normal path, never a shortcut to review
    - anything else -> report only
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
        if not res.run_id:
            return {
                "dispatch_id": dispatch_id, "resolved": "none",
                "reason": "no run_id on the receipt — nothing proves the "
                          "execution state; left unknown pending "
                          "investigation",
            }
        try:
            run = _control_client(policy).get_run(res.run_id)
        except WrapperError as exc:
            return {
                "dispatch_id": dispatch_id, "resolved": "none",
                "run_status": "unreachable",
                "reason": f"control plane unreachable: {exc}",
            }
        if run is None:
            return {
                "dispatch_id": dispatch_id, "resolved": "none",
                "run_status": "not_found",
                "reason": "the wrapper reports no such run — absence is not "
                          "proof nothing executed; left unknown pending "
                          "external investigation",
            }
        # Canonical context must equal the receipt's reserved context — a
        # foreign run view is never verified, never promoted. Same echo
        # contract as _handle_run_status, plus the reserved attempt id.
        anomalies = []
        if run.get("task_id") != res.task_id:
            anomalies.append(
                f"task_id {run.get('task_id')!r} != receipt {res.task_id!r}")
        workspace = policy.workspaces.get(res.workspace_id)
        submit_ws = (
            workspace.wrapper_workspace_id
            if workspace is not None and workspace.wrapper_workspace_id
            else res.workspace_id
        )
        if run.get("workspace_id") != submit_ws:
            anomalies.append(
                f"workspace_id {run.get('workspace_id')!r} != submitted "
                f"{submit_ws!r}")
        if res.attempt_id is not None and run.get("attempt_id") != res.attempt_id:
            anomalies.append(
                f"attempt_id {run.get('attempt_id')!r} != reserved "
                f"{res.attempt_id!r}")
        if policy.dispatch.send_execution_metadata:
            expected_execution = {
                "task_revision": res.task_revision,
                "base_revision": res.base_revision,
                "route": res.route,
                "policy_version": policy.policy_version,
            }
            if run.get("execution") != expected_execution:
                anomalies.append(
                    "execution metadata echo differs from the reserved "
                    "context")
        if anomalies:
            raise ControlError(
                "run context mismatch — refusing to verify a foreign run: "
                + "; ".join(anomalies)
            )
        status = str(run.get("status") or "unknown")
        if status in ("failed", "cancelled"):
            store.transition(
                dispatch_id, CANCELLED if status == "cancelled" else FAILED,
                detail=f"resolved by {actor}: wrapper run {status}")
            return {"dispatch_id": dispatch_id, "resolved": status,
                    "run_status": status}
        if status != "completed":
            return {"dispatch_id": dispatch_id, "resolved": "none",
                    "run_status": status}
        if run.get("outcome") != "succeeded":
            store.transition(
                dispatch_id, FAILED,
                detail=f"resolved by {actor}: run completed with outcome "
                       f"{run.get('outcome')!r} — not promotable")
            return {"dispatch_id": dispatch_id, "resolved": "failed",
                    "run_status": status,
                    "reason": "outcome is not 'succeeded'"}
        if res.cancel_requested:
            store.transition(dispatch_id, CANCELLED,
                             detail=f"resolved by {actor}: cancel was "
                                    "already requested")
            return {"dispatch_id": dispatch_id, "resolved": "cancelled",
                    "run_status": status}

        # completed + succeeded: the SAME verification bar as dispatch —
        # intact bound worktree, full-argv verification, artifacts, diff,
        # scope — before any review handoff.
        workspace = policy.workspaces.get(res.workspace_id)
        if workspace is None:
            raise ControlError(
                f"receipt workspace {res.workspace_id!r} is not in the "
                "current policy — cannot verify"
            )
        with KernelBridge(board_db, cfg=policy.hermes) as kernel:
            task_dict = kernel.call(
                "get_task", task_id=res.task_id).get("task")
        spec = _resolve_spec_for(policy, policy_path, task_dict or {"id": res.task_id})
        if spec is None:
            raise ControlError(
                f"cannot resolve a spec for {res.task_id} — verification "
                "cannot run without it; receipt stays unknown"
            )
        # The verification contract that gated the run is the RESERVED one:
        # the card body is an untrusted channel, so the spec it yields now
        # must hash to exactly what the reservation recorded — and the
        # policy to the reserved fingerprint. Contract drift refuses; the
        # run is never re-verified against a weaker, edited contract and
        # fresh checks are never labelled with the old fingerprint.
        from .dispatch import _spec_hash
        current_spec_hash = _spec_hash(spec)
        if current_spec_hash != res.spec_hash:
            raise ControlError(
                f"resolve contract drift: the card's spec now hashes "
                f"{current_spec_hash} but the receipt reserved "
                f"{res.spec_hash} — the run stays bound to the reserved "
                "contract; receipt left unknown for manual review"
            )
        current_fp = policy_fingerprint(policy)
        if current_fp != res.policy_fingerprint:
            raise ControlError(
                f"resolve contract drift: policy fingerprint "
                f"{current_fp} != reserved {res.policy_fingerprint} — "
                "receipt left unknown for manual review"
            )
        if not res.worktree or not Path(res.worktree).is_dir():
            with KernelBridge(board_db, cfg=policy.hermes) as kernel:
                kernel.call(
                    "block_owned", task_id=res.task_id, kind="needs_input",
                    run_id=res.kernel_run_id,
                    reason=f"resolve {dispatch_id}: recorded worktree "
                           f"{res.worktree!r} is gone — cannot verify the "
                           "run's output; left unknown",
                )
            raise ControlError(
                f"recorded worktree {res.worktree!r} is missing — the run's "
                "output cannot be verified; receipt stays unknown"
            )
        wt = Worktree(path=Path(res.worktree).resolve(), branch=res.branch,
                      base_revision=res.base_revision,
                      workspace_id=res.workspace_id)
        try:
            repo = resolve_repo(workspace.repo)
            if _git_dir_of(wt.path) != _git_dir_of(repo):
                raise WorktreeError("worktree belongs to a different repo")
        except WorktreeError as exc:
            raise ControlError(f"worktree validation failed: {exc}")
        secrets = []
        exe = policy.execution
        for attr in ("credential_file", "control_credential_file"):
            path = getattr(exe, attr, None)
            if path:
                try:
                    v = Path(path).read_text(encoding="utf-8").strip()
                except OSError:
                    v = None
                if v:
                    secrets.append(v)
        try:
            verify = run_verification(
                wt, spec.verification.argv,
                executables=policy.verification.executables,
                commands=policy.verification.commands,
                timeout_seconds=policy.verification.timeout_seconds,
                max_output_bytes=policy.verification.max_output_bytes,
            )
            artifacts, missing = collect_artifacts(
                wt, spec.artifacts, max_bytes=policy.limits.max_spec_bytes
            )
            changed = changed_files(wt)
            violations = check_scope(changed, list(spec.allowed_scope))
            diff = capture_diff(wt, policy.verification.max_diff_bytes)
        except (WorktreeError, OSError) as exc:
            # Inspection failure fails CLOSED — never a successful review,
            # never swallowed uncertainty.
            reason = (f"quality_failed (resolve): evidence inspection "
                      f"failed — {exc} [dispatch {res.dispatch_id}]")
            with KernelBridge(board_db, cfg=policy.hermes) as kernel:
                kernel.call(
                    "block_owned", task_id=res.task_id, kind="needs_input",
                    run_id=res.kernel_run_id, reason=reason)
            store.transition(dispatch_id, BLOCKED, detail=reason)
            return {"dispatch_id": dispatch_id, "resolved": "blocked",
                    "run_status": status, "reason": reason}
        diff_info = persist_diff(store.path, res.dispatch_id, diff, secrets)
        artifact_entries = []
        for artifact in artifacts:
            try:
                digest, size = sha256_file(
                    artifact, max_bytes=policy.limits.max_spec_bytes)
            except WorktreeError:
                continue
            artifact_entries.append(
                {"path": str(artifact), "sha256": digest, "bytes": size})
        store.record_evidence(res.dispatch_id, {
            "verification": {
                "argv": list(spec.verification.argv),
                "exit_code": verify.exit_code,
                "output": sanitize_text(verify.output, secrets)[-4096:],
                "truncated": verify.truncated,
                "timed_out": verify.timed_out,
            },
            "artifacts": artifact_entries,
            "diff": diff_info,
            "changed_files": changed,
            "scope_violations": violations,
            "base_revision": spec.base_revision,
            "policy_fingerprint": res.policy_fingerprint,
            "resolved_by": actor,
        })
        if not verify.ok or missing or violations:
            reasons = []
            if not verify.ok:
                reasons.append(
                    f"verification failed (exit={verify.exit_code}): "
                    f"{sanitize_text(verify.output, secrets)[-400:]}")
            if missing:
                reasons.append(f"missing artifacts: {missing}")
            if violations:
                reasons.append(
                    f"changed files outside allowed_scope: {violations}")
            reason = ("quality_failed (resolve): " + "; ".join(reasons)
                      + f" [dispatch {res.dispatch_id}]")
            with KernelBridge(board_db, cfg=policy.hermes) as kernel:
                kernel.call(
                    "block_owned", task_id=res.task_id, kind="needs_input",
                    run_id=res.kernel_run_id, reason=reason)
            store.transition(dispatch_id, BLOCKED, detail=reason)
            return {"dispatch_id": dispatch_id, "resolved": "blocked",
                    "run_status": status, "reason": reason}

        # Review handoff — every board mutation fenced to THIS receipt's
        # kernel run. A card whose ownership cannot be proven is an
        # explicit manual-review hold, never an unfenced promotion and
        # never a silent unblock of a foreign operator block.
        with KernelBridge(board_db, cfg=policy.hermes) as kernel:
            task = kernel.call("get_task", task_id=res.task_id).get("task")
            if task is None:
                raise ControlError(f"card {res.task_id} is missing")
            ours = res.kernel_run_id
            held = None
            if ours is None:
                held = ("receipt has no kernel run — card run ownership is "
                        "unprovable")
            if held is None and task["status"] == "blocked":
                lb = task.get("last_block") or {}
                block_reason = lb.get("reason") or ""
                block_run = lb.get("run_id")
                if block_run is None or int(block_run) != int(ours):
                    held = ("card is blocked on a run this dispatch did "
                            "not own — never auto-cleared")
                elif not _dispatch_block_marked(block_reason,
                                                res.dispatch_id):
                    held = ("card is blocked by a block this dispatch did "
                            "not file — never auto-cleared")
                elif (task.get("latest_run_id") is not None
                      and int(task["latest_run_id"]) != int(ours)):
                    held = ("a newer kernel run owns the card — the stale "
                            "run's resolve cannot promote it")
                else:
                    # Fenced under the mutator's own write lock: the card
                    # must still be blocked, still carry no newer/foreign
                    # run, and still wear the exact block this dispatch
                    # filed — a changed or foreign block is never cleared.
                    ub = kernel.call(
                        "unblock_owned", task_id=res.task_id, run_id=ours,
                        latest_run_id=task.get("latest_run_id"),
                        reason_prefix=f"resolve {res.dispatch_id}:",
                        reason_suffix=f"[dispatch {res.dispatch_id}]",
                    )
                    if not ub.get("unblocked"):
                        held = ("the fenced unblock was refused — card "
                                "state changed under us: "
                                f"{ub.get('reason') or 'unknown'}")
                    else:
                        task = kernel.call(
                            "get_task", task_id=res.task_id).get("task")
            expected = None
            if held is None:
                cur = task.get("current_run_id") if task else None
                latest = task.get("latest_run_id") if task else None
                if task["status"] == "running":
                    if cur is None or int(cur) != int(ours):
                        held = ("card runs under a different kernel run — "
                                "ownership lost")
                    else:
                        expected = ours
                elif task["status"] in ("ready", "todo"):
                    if cur is not None and int(cur) != int(ours):
                        held = "a foreign kernel run is live on the card"
                    elif latest is not None and int(latest) != int(ours):
                        held = ("a newer kernel run owns the card — the "
                                "stale run's resolve cannot promote it")
                    else:
                        # Re-claim to obtain a live, provable fence. The
                        # fenced claim re-proves under the kernel's own
                        # write lock that no run newer than ours exists —
                        # an interleaved foreign claim cannot be ridden
                        # over. The NEW run id is recorded on the receipt
                        # so every later control refers to the real
                        # handoff run.
                        claim = kernel.call(
                            "claim", task_id=res.task_id,
                            claimer=f"jev-resolve:{dispatch_id}",
                            ttl_seconds=policy.dispatch.claim_ttl_seconds,
                            expected_run_id=ours,
                        )
                        if claim.get("claimed"):
                            expected = (claim.get("task") or {}).get(
                                "current_run_id")
                            try:
                                store.transition(
                                    dispatch_id, UNKNOWN,
                                    kernel_run_id=expected,
                                    detail=f"resolve {dispatch_id}: "
                                           f"re-claimed under kernel run "
                                           f"{expected}")
                            except StoreError:
                                held = ("receipt closed while claiming "
                                        "the resolve run")
                        else:
                            held = ("card could not be claimed for the "
                                    "resolve handoff" +
                                    (f": {claim.get('reason')}"
                                     if claim.get("reason") else ""))
                else:
                    held = (f"card status {task['status']!r} cannot accept "
                            "this run's resolve")
            if held is not None:
                return {
                    "dispatch_id": dispatch_id, "resolved": "held",
                    "run_status": status,
                    "reason": f"{held} — receipt stays unknown pending "
                              "manual review",
                }
            resp = kernel.call(
                "request_review", task_id=res.task_id,
                summary=f"resolved unknown dispatch {dispatch_id}: run "
                        f"{res.run_id} completed and re-verified",
                metadata={"dispatch_id": dispatch_id,
                          "wrapper_run_id": res.run_id,
                          "wrapper_attempt_id": res.attempt_id,
                          "route": res.route,
                          "resolved_by": actor},
                expected_run_id=expected,
            )
            if not resp["ok"]:
                raise ControlError(
                    f"review handoff refused during resolve: "
                    f"{resp.get('reason')}"
                )
        try:
            store.transition(dispatch_id, REVIEW, require_no_cancel=True,
                             detail=f"resolved by {actor}: run completed, "
                                    "re-verified")
        except StoreError:
            with KernelBridge(board_db, cfg=policy.hermes) as kernel:
                try:
                    kernel.call("reopen_review_if", task_id=res.task_id,
                                expected_run_id=expected)
                except KernelError:
                    pass
            raise ControlError(
                "a cancellation landed during the resolve handoff — the "
                "card was pulled back from review; receipt stays cancelled"
            )
        return {"dispatch_id": dispatch_id, "resolved": "review",
                "run_status": status}
    finally:
        store.close()
