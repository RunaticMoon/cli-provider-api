"""Thin one-shot dispatcher — the Jev execution lane.

``dispatch_once`` takes the kernel's singleton dispatch-tick lock, recovers
stale receipts, then processes *ready* cards in the ``jev-native`` scope:

    reserve -> claim (max_retries=1) -> worktree -> submit -> verify -> review

It never schedules, never retries, never selects a backend order, and never
marks a card ``done`` — a finished worker goes to ``review`` under the
expected-run fence; only a separate verified ``accept`` completes it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .classifier import classify
from .kernel import KernelBridge, KernelError
from .models import JevDecision, RecommendedAction, TaskSpec
from .policy import (
    EffortUnsupported,
    Policy,
    load_policy,
    policy_fingerprint,
    resolve_effort,
)
from .spec import resolve_spec
from .store import (
    ABORTED,
    BLOCKED,
    CANCELLED,
    CLAIMED,
    COMPLETING,
    FAILED,
    RESERVED,
    REVIEW,
    SUBMITTED,
    UNKNOWN,
    DispatchStore,
    ReservationExists,
)
from .worktree import (
    WorktreeError,
    capture_diff,
    check_dependencies,
    collect_artifacts,
    prepare_worktree,
    resolve_repo,
    run_verification,
)
from .wrapper_client import (
    WrapperClient,
    WrapperError,
    WrapperHTTPError,
    WrapperTransportError,
)


class DispatchError(Exception):
    """Dispatch precondition failure (bad policy, missing target, no lock)."""


def _spec_hash(spec: TaskSpec) -> str:
    canonical = json.dumps(
        spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def _prompt(spec: TaskSpec) -> str:
    """Deterministic prompt for the single model request."""
    lines = [
        f"Task: {spec.objective}",
        "",
        "Inputs:",
        *[f"- {i}" for i in spec.inputs],
        "",
        "Relevant files:",
        *[f"- {f}" for f in spec.relevant_files],
        "",
        "Allowed scope:",
        *[f"- {s}" for s in spec.allowed_scope],
        "",
        "Acceptance criteria:",
        *[f"- {c}" for c in spec.acceptance_criteria],
    ]
    if spec.prohibited:
        lines += ["", "Prohibited:", *[f"- {p}" for p in spec.prohibited]]
    return "\n".join(lines)


def _task_board_fields(raw: dict) -> dict:
    """Adapt a bridge task dict to the classifier's BoardTask shape."""
    return {
        "id": raw["id"],
        "body": raw.get("body"),
        "title": raw.get("title") or "",
        "assignee": raw.get("assignee"),
        "status": raw.get("status") or "ready",
        "priority": raw.get("priority") or 0,
        "created_at": raw.get("created_at") or 0,
        "workspace_kind": raw.get("workspace_kind") or "scratch",
        "workspace_path": raw.get("workspace_path"),
        "model_override": raw.get("model_override"),
        "provider_override": raw.get("provider_override"),
        "reasoning_effort": raw.get("reasoning_effort"),
        "skills": None,
        "max_retries": raw.get("max_retries"),
        "max_runtime_seconds": None,
        "parents": tuple(raw.get("parents") or ()),
    }


def _integrated_revision(task: dict | None) -> str | None:
    """A dependency's recorded integrated revision: the closing run's
    metadata first, then a JSON ``result`` body."""
    if not task:
        return None
    meta = task.get("last_run_metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    if isinstance(meta, dict) and meta.get("integrated_revision"):
        return str(meta["integrated_revision"])
    result = task.get("result")
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except ValueError:
            return None
        if isinstance(parsed, dict) and parsed.get("integrated_revision"):
            return str(parsed["integrated_revision"])
    return None


def dispatch_once(
    *,
    board_db: str | Path,
    policy_path: str | Path,
    store_path: str | Path,
    client: WrapperClient | None = None,
) -> dict:
    """One dispatch tick. Returns a JSON-serializable report."""
    policy = load_policy(policy_path)
    report: dict = {
        "schema_version": 1,
        "mode": "dispatch",
        "board_db": str(board_db),
        "policy_version": policy.policy_version,
        "policy_fingerprint": policy_fingerprint(policy),
        "skipped_locked": False,
        "recovered": [],
        "results": [],
    }
    if policy.execution is None:
        raise DispatchError(
            "policy has no execution target — dispatch needs execution.mode/"
            "base_url (direct wrapper or gateway)"
        )
    from .shadow import _load_task_map
    task_map = _load_task_map(policy, Path(policy_path))
    if client is None:
        client = WrapperClient(
            policy.execution.base_url,
            credential_file=policy.execution.credential_file,
            timeout_seconds=policy.dispatch.http_timeout_seconds,
        )

    store = DispatchStore(store_path)
    try:
        with KernelBridge(board_db, cfg=policy.hermes) as kernel:
            if not kernel.acquire_lock():
                report["skipped_locked"] = True
                return report
            _recover_stale(kernel, store, report)

            scope = kernel.call(
                "list_scope",
                assignee=policy.scope.assignee,
                statuses=["ready"],  # dispatch processes READY cards only
                limit=policy.limits.max_cards,
            )["tasks"]

            dispatched = 0
            for raw in scope:
                if dispatched >= policy.dispatch.max_cards_per_tick:
                    break
                if store.live_for_task(raw["id"]) is not None:
                    report["results"].append(
                        {"task_id": raw["id"], "action": "skipped",
                         "reason": "live_or_unknown_reservation_exists"}
                    )
                    continue
                outcome = _dispatch_card(
                    kernel, store, client, policy, raw, task_map
                )
                report["results"].append(outcome)
                if outcome.get("dispatched"):
                    dispatched += 1
    finally:
        store.close()
    return report


def _recover_stale(kernel: KernelBridge, store: DispatchStore, report: dict) -> None:
    """Crash recovery BEFORE new work: reservations left live by a dead
    process are reconciled without replaying anything."""
    for res in store.list_live():
        if res.state == RESERVED:
            # Persisted pre-claim; the claim never happened -> nothing ran.
            store.transition(res.dispatch_id, ABORTED,
                             detail="crashed before kernel claim")
            report["recovered"].append(
                {"dispatch_id": res.dispatch_id, "state": "aborted"})
        else:
            # claimed/submitted/completing — execution may have started.
            store.transition(res.dispatch_id, UNKNOWN,
                             detail="crashed with execution possibly in "
                                    "flight — never replayed")
            kernel.call(
                "block", task_id=res.task_id, kind="needs_input",
                reason=f"dispatch {res.dispatch_id} unknown after crash — "
                       "execution may have started; manual resolve required",
            )
            report["recovered"].append(
                {"dispatch_id": res.dispatch_id, "state": "unknown"})


def _record_block(
    kernel, store, res, *, kind: str, reason: str
) -> dict:
    """Block the card (typed kernel kind) and close the receipt."""
    kernel.call("block", task_id=res.task_id, kind=kind, reason=reason)
    store.transition(res.dispatch_id, BLOCKED, detail=reason)
    return {"task_id": res.task_id, "dispatch_id": res.dispatch_id,
            "action": "blocked", "reason": reason}


def _dispatch_card(kernel, store, client, policy: Policy, raw: dict,
                   task_map) -> dict:
    from .board import BoardTask

    task = BoardTask(**_task_board_fields(raw))
    spec_result = resolve_spec(
        task,
        task_map,
        max_body_bytes=policy.limits.max_body_bytes,
        max_spec_bytes=policy.limits.max_spec_bytes,
    )
    decision = classify(task, spec_result, policy)
    action = decision.recommended_action

    if action is RecommendedAction.HOLD:
        # Transient: leave the card ready, record nothing.
        return {"task_id": task.id, "action": "hold", "reason": decision.reason}

    if action is RecommendedAction.REPLAN:
        kernel.call("block", task_id=task.id, kind="needs_input",
                    reason=f"replan: {decision.reason}")
        return {"task_id": task.id, "action": "replan",
                "reason": decision.reason}

    if action is RecommendedAction.NEEDS_APPROVAL:
        return _gate_for_approval(kernel, store, policy, task, decision)

    return _execute(kernel, store, client, policy, task, spec_result.spec,
                    decision)


def _gate_for_approval(kernel, store, policy, task, decision) -> dict:
    """needs_approval -> durable approval record + typed block + notify."""
    approval = store.create_approval(
        task_id=task.id,
        task_revision=decision.task_revision,
        operation="dispatch",
        run_id=None,
        allowed_actors=list(policy.control.operators),
        ttl_seconds=policy.approval.expiry_seconds,
    )
    kernel.call(
        "block", task_id=task.id, kind="needs_input",
        reason=f"needs_approval ({approval.approval_id}): {decision.reason}",
    )
    if policy.approval.notify is not None:
        kernel.call(
            "notify_sub", task_id=task.id,
            platform=policy.approval.notify.platform,
            chat_id=policy.approval.notify.chat_id,
            thread_id=policy.approval.notify.thread_id,
            user_id=policy.approval.notify.user_id,
        )
    return {"task_id": task.id, "action": "needs_approval",
            "approval_id": approval.approval_id, "reason": decision.reason}


def _execute(kernel, store, client, policy, task, spec, decision) -> dict:
    route = policy.routes[decision.route]
    workspace = policy.workspaces[spec.workspace_id]
    fingerprint = policy_fingerprint(policy)

    try:
        effort = resolve_effort(
            policy, route, decision.capability, spec.effort_hint
        )
    except EffortUnsupported as exc:
        kernel.call("block", task_id=task.id, kind="needs_input",
                    reason=f"unsupported effort mapping: {exc}")
        return {"task_id": task.id, "action": "blocked",
                "reason": f"effort_unsupported: {exc}"}

    # 1. Stable reservation BEFORE any HTTP/worktree side effect.
    try:
        res = store.reserve(
            task_id=task.id,
            task_revision=spec.task_revision,
            spec_hash=_spec_hash(spec),
            policy_fingerprint=fingerprint,
            workspace_id=spec.workspace_id,
            base_revision=spec.base_revision,
            route=decision.route,
        )
    except ReservationExists:
        return {"task_id": task.id, "action": "skipped",
                "reason": "live_or_unknown_reservation_exists"}

    try:
        return _execute_reserved(
            kernel, store, client, policy, task, spec, decision,
            res, effort, workspace,
        )
    except KernelError as exc:
        # Kernel call failed mid-flow — we cannot prove what ran.
        store.transition(res.dispatch_id, UNKNOWN,
                         detail=f"kernel error: {exc}")
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "unknown", "reason": str(exc)}


def _execute_reserved(kernel, store, client, policy, task, spec, decision,
                      res, effort, workspace) -> dict:
    try:
        repo = resolve_repo(workspace.repo)
    except WorktreeError as exc:
        return _record_block(kernel, store, res, kind="needs_input",
                             reason=f"workspace validation failed: {exc}")

    # 2. Dependency proof BEFORE claim: done alone is insufficient — each
    #    parent needs an integrated_revision that is an ancestor of base.
    deps = []
    for parent_id in task.parents:
        pinfo = kernel.call("get_task", task_id=parent_id).get("task")
        deps.append((parent_id, _integrated_revision(pinfo),
                     (pinfo or {}).get("status")))
    undone = [pid for pid, _rev, status in deps if status != "done"]
    if undone:
        # Waiting, not failing: leave the card ready (kernel claim would demote).
        store.transition(res.dispatch_id, ABORTED,
                         detail=f"dependencies not done: {undone}")
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "skipped", "reason": f"dependencies undone: {undone}"}
    checks = check_dependencies(
        repo, spec.base_revision, [(pid, rev) for pid, rev, _s in deps]
    )
    failed = [c for c in checks if not c.ok]
    if failed:
        reason = "; ".join(f"{c.task_id}: {c.reason}" for c in failed)
        return _record_block(
            kernel, store, res, kind="dependency",
            reason=f"dependency integrated_revision check failed: {reason}",
        )

    # 3. max_retries=1 before claim — stock reclaim must never replay an
    #    external write.
    kernel.call("set_max_retries", task_id=task.id, value=1)

    # 4. Atomic claim.
    claimer = f"jev:{res.dispatch_id}"
    claimed = kernel.call(
        "claim", task_id=task.id,
        ttl_seconds=policy.dispatch.claim_ttl_seconds,
        claimer=claimer,
    )
    if not claimed["claimed"]:
        store.transition(res.dispatch_id, ABORTED,
                         detail="claim lost (claimed elsewhere or demoted)")
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "skipped", "reason": "claim lost"}
    krun = (claimed["task"] or {}).get("current_run_id")
    res = store.transition(res.dispatch_id, CLAIMED, kernel_run_id=krun)

    # 5. Per-card worktree at the pinned base; partial state preserved on error.
    try:
        wt = prepare_worktree(
            repo=repo,
            worktree_root=Path(workspace.worktree_root),
            workspace_id=spec.workspace_id,
            dispatch_id=res.dispatch_id,
            base_revision=spec.base_revision,
        )
    except WorktreeError as exc:
        return _record_block(kernel, store, res, kind="needs_input",
                             reason=f"worktree setup failed: {exc}")
    res = store.transition(res.dispatch_id, CLAIMED,
                           branch=wt.branch, worktree=str(wt.path))

    # 6. Submit the single model request — full JSON body, never first byte.
    #    Direct mode: the operator-declared preset alias. Gateway mode: the
    #    compiled combo name (candidate order lives in 9Router, not here).
    model = policy.execution.model or f"jev.{decision.route}"
    execution_meta = None
    if policy.dispatch.send_execution_metadata:
        execution_meta = {
            "task_revision": spec.task_revision,
            "base_revision": spec.base_revision,
            "route": decision.route,
            "policy_version": policy.policy_version,
        }
    kernel.call("heartbeat", task_id=task.id, claimer=claimer,
                ttl_seconds=policy.dispatch.claim_ttl_seconds)
    try:
        outcome = client.submit_chat(
            model=model,
            task_id=task.id,
            workspace_id=workspace.wrapper_workspace_id or spec.workspace_id,
            messages=[{"role": "user", "content": _prompt(spec)}],
            execution=execution_meta,
        )
    except WrapperTransportError as exc:
        # Unconfirmed transport: the request may have reached the wrapper.
        kernel.call("block", task_id=task.id, kind="needs_input",
                    expected_run_id=krun,
                    reason=f"transport unconfirmed — run state unknown: {exc}")
        store.transition(res.dispatch_id, UNKNOWN, detail=str(exc))
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "unknown", "reason": str(exc)}
    except WrapperHTTPError as exc:
        run = exc.run
        if run and run.get("run_id"):
            # The wrapper rejected AFTER creating/attaching a run — reconcile.
            res = store.transition(res.dispatch_id, SUBMITTED,
                                   run_id=run["run_id"],
                                   attempt_id=run.get("attempt_id"))
            return _handle_run_status(
                kernel, store, client, policy, task, spec, res, wt,
                str(run.get("status") or "unknown"), run, claimer, krun,
            )
        # Rejected before any run existed (auth/validation): no execution.
        return _record_block(kernel, store, res, kind="needs_input",
                             reason=f"wrapper rejected the request: {exc}",
                             )
    res = store.transition(res.dispatch_id, SUBMITTED,
                           run_id=outcome.run_id, attempt_id=outcome.attempt_id)
    return _handle_run_status(
        kernel, store, client, policy, task, spec, res, wt,
        outcome.status, outcome.run, claimer, krun,
    )


def _handle_run_status(kernel, store, client, policy, task, spec, res, wt,
                       status, run, claimer, krun) -> dict:
    """Terminal run state -> verify -> review (or typed block). Never done."""
    if status == "cancelled":
        store.transition(res.dispatch_id, CANCELLED, detail="run cancelled")
        kernel.call("block", task_id=task.id, kind="needs_input",
                    expected_run_id=krun,
                    reason=f"wrapper run {res.run_id} cancelled")
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "cancelled", "run_id": res.run_id}
    if status in ("failed", "unknown"):
        state = UNKNOWN if status == "unknown" else FAILED
        kernel.call("block", task_id=task.id, kind="needs_input",
                    expected_run_id=krun,
                    reason=f"wrapper run {res.run_id} ended {status}: "
                           f"{(run.get('detail') or '')[:200]}")
        store.transition(res.dispatch_id, state,
                         detail=f"run {status}")
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": status, "run_id": res.run_id}

    # completed — but a persisted cancel intent wins (late-completion guard).
    res = store.transition(res.dispatch_id, COMPLETING)
    if res.cancel_requested:
        client.cancel_run(res.run_id)  # best-effort; intent already durable
        kernel.call("block", task_id=task.id, kind="needs_input",
                    expected_run_id=krun,
                    reason="cancel requested before review handoff")
        store.transition(res.dispatch_id, CANCELLED,
                         detail="cancelled before review")
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "cancelled", "run_id": res.run_id}

    # Verification: real checks only — trusted argv, bounded capture.
    verify = run_verification(
        wt, spec.verification.argv,
        executables=policy.verification.executables,
        timeout_seconds=policy.verification.timeout_seconds,
        max_output_bytes=policy.verification.max_output_bytes,
    )
    artifacts, missing = collect_artifacts(
        wt, spec.artifacts, max_bytes=policy.limits.max_spec_bytes
    )
    diff = capture_diff(wt, policy.verification.max_diff_bytes)
    if not verify.ok or missing:
        reasons = []
        if not verify.ok:
            reasons.append(
                f"verification failed (exit={verify.exit_code}): "
                f"{verify.output[-400:]}"
            )
        if missing:
            reasons.append(f"missing artifacts: {missing}")
        reason = "quality_failed: " + "; ".join(reasons)
        kernel.call("block", task_id=task.id, kind="needs_input",
                    expected_run_id=krun, reason=reason)
        store.transition(res.dispatch_id, BLOCKED, detail=reason)
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "blocked", "reason": reason,
                "dispatched": True}

    # Handoff to review under the expected-run fence. Artifacts stage durably.
    resp = kernel.call(
        "request_review", task_id=task.id,
        summary=(
            f"jev dispatch {res.dispatch_id}: run {res.run_id} completed; "
            "verification passed"
        ),
        metadata={
            "dispatch_id": res.dispatch_id,
            "wrapper_run_id": res.run_id,
            "wrapper_attempt_id": res.attempt_id,
            "route": res.route,
            "artifacts": [a.name for a in artifacts],
            "diff_bytes": len(diff.encode("utf-8", "replace")),
        },
        expected_run_id=krun,
    )
    if not resp["ok"]:
        # Fence lost or kernel refused — do NOT guess; unknown, never replay.
        store.transition(res.dispatch_id, UNKNOWN,
                         detail=f"request_review refused: {resp.get('reason')}")
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "unknown",
                "reason": f"review handoff refused: {resp.get('reason')}",
                "dispatched": True}
    store.transition(res.dispatch_id, REVIEW,
                     detail=f"run {res.run_id} verified; awaiting Lead review")
    if policy.approval.notify is not None:
        kernel.call(
            "notify_sub", task_id=task.id,
            platform=policy.approval.notify.platform,
            chat_id=policy.approval.notify.chat_id,
            thread_id=policy.approval.notify.thread_id,
            user_id=policy.approval.notify.user_id,
        )
    return {"task_id": task.id, "dispatch_id": res.dispatch_id,
            "action": "review", "run_id": res.run_id, "dispatched": True}
