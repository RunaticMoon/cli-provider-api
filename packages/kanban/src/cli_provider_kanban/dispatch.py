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
import threading
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
    GrantMismatch,
    ReservationExists,
    StoreError,
)
from .worktree import (
    WorktreeError,
    capture_diff,
    changed_files,
    check_dependencies,
    check_scope,
    collect_artifacts,
    prepare_worktree,
    resolve_repo,
    run_verification,
    sanitize_text,
    sha256_bytes,
    sha256_file,
    validate_prepared_worktree,
    validate_runner_binding,
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
    """Deterministic prompt for the single model request — carries the full
    contract: base revision, dependencies, deliverables and the verification
    criteria the result will be held to."""
    lines = [
        f"Task: {spec.objective}",
        "",
        f"Base revision: {spec.base_revision}",
    ]
    if spec.dependency_ids:
        lines += ["", "Depends on:", *[f"- {d}" for d in spec.dependency_ids]]
    lines += [
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
    if spec.artifacts:
        lines += ["", "Deliverables (must exist afterwards):",
                  *[f"- {a}" for a in spec.artifacts]]
    if spec.verification is not None:
        lines += [
            "",
            "Verification that will gate this work:",
            f"- argv: {' '.join(spec.verification.argv)}",
            f"- criteria: {spec.verification.criteria}",
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
    # Run-control calls (cancel) go to the CONTROL target — gateway mode
    # separates it from the data POST URL; direct mode defaults to base.
    if client is not None and policy.execution.control_base_url in (
        None, policy.execution.base_url
    ):
        control_client = client
    else:
        control_client = WrapperClient(
            policy.execution.control_base_url or policy.execution.base_url,
            credential_file=(
                policy.execution.control_credential_file
                or policy.execution.credential_file
            ),
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
                    kernel, store, client, control_client, policy, raw,
                    task_map
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
        if res.state == UNKNOWN:
            continue  # already recovered once; card already blocked
        if res.state == RESERVED:
            # Persisted pre-claim; the claim never happened -> nothing ran.
            try:
                store.transition(res.dispatch_id, ABORTED,
                                 detail="crashed before kernel claim")
            except StoreError:
                pass  # a racing cancel already closed it
            report["recovered"].append(
                {"dispatch_id": res.dispatch_id, "state": "aborted"})
        else:
            # claimed/submitted/completing — execution may have started.
            try:
                store.transition(res.dispatch_id, UNKNOWN,
                                 detail="crashed with execution possibly in "
                                        "flight — never replayed")
            except StoreError:
                pass  # a racing cancel already closed it
            kernel.call(
                "block", task_id=res.task_id, kind="needs_input",
                reason=f"dispatch {res.dispatch_id} unknown after crash — "
                       "execution may have started; manual resolve required",
            )
            report["recovered"].append(
                {"dispatch_id": res.dispatch_id, "state": "unknown"})


def _record_block(
    kernel, store, res, *, kind: str, reason: str,
    control_client=None, task=None, krun=None,
) -> dict:
    """Block the card (typed kernel kind) and close the receipt — a
    racing cancel wins the receipt instead of crashing the tick."""
    kernel.call("block", task_id=res.task_id, kind=kind, reason=reason)
    if control_client is not None and task is not None:
        _res, cancelled = _transition_guarded(
            store, control_client, kernel, res, task, krun, BLOCKED, {},
            detail=reason,
        )
        if cancelled:
            return cancelled
    else:
        try:
            store.transition(res.dispatch_id, BLOCKED, detail=reason)
        except StoreError:
            cur = store.get(res.dispatch_id)
            if cur is not None and cur.cancel_requested:
                return {"task_id": res.task_id,
                        "dispatch_id": res.dispatch_id,
                        "action": "cancelled", "reason": reason}
            raise
    return {"task_id": res.task_id, "dispatch_id": res.dispatch_id,
            "action": "blocked", "reason": reason}


def _dispatch_card(kernel, store, client, control_client,
                   policy: Policy, raw: dict, task_map) -> dict:
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
        # An applied grant bound to THIS exact scope (task revision + spec
        # hash + policy fingerprint) lets the card proceed — consumed
        # atomically with the reservation. Anything else re-gates durably.
        grant = store.find_applied_grant(
            task_id=task.id, operation="dispatch",
            task_revision=decision.task_revision,
            spec_hash=_spec_hash(spec_result.spec),
            policy_fingerprint=policy_fingerprint(policy),
        )
        if grant is not None:
            return _execute(kernel, store, client, control_client, policy,
                            task, spec_result.spec, decision, grant=grant)
        return _gate_for_approval(kernel, store, policy, task, decision,
                                  spec_result.spec)

    return _execute(kernel, store, client, control_client, policy, task,
                    spec_result.spec, decision, grant=None)


def _gate_for_approval(kernel, store, policy, task, decision, spec) -> dict:
    """needs_approval -> durable approval record + typed block + notify.

    The record binds the exact scope (task revision + spec hash + policy
    fingerprint) so a changed card or policy can never ride an old grant.
    """
    approval = store.create_approval(
        task_id=task.id,
        task_revision=decision.task_revision,
        spec_hash=_spec_hash(spec),
        policy_fingerprint=policy_fingerprint(policy),
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


def _candidate_models(policy: Policy, route, capability: str) -> list[str]:
    """``driver:model`` descriptors of the candidates that could actually
    serve — checked against the Runner binding's ``allowed_models`` pin."""
    out = []
    for bid in route.candidates:
        backend = policy.backend_map()[bid]
        if backend.enabled and backend.capabilities.get(capability) is True:
            out.append(f"{backend.driver}:{backend.model}")
    return out


def _execute(kernel, store, client, control_client, policy, task, spec,
             decision, grant=None) -> dict:
    if decision.route not in policy.routes:
        kernel.call("block", task_id=task.id, kind="needs_input",
                    reason=f"classifier produced unroutable route "
                           f"{decision.route!r} — no policy route binds it")
        return {"task_id": task.id, "action": "blocked",
                "reason": f"unroutable route {decision.route!r}"}
    route = policy.routes[decision.route]
    workspace = policy.workspaces.get(spec.workspace_id)
    if workspace is None:
        kernel.call("block", task_id=task.id, kind="needs_input",
                    reason=f"spec workspace {spec.workspace_id!r} is not in "
                           "the policy — refusing to dispatch into an "
                           "unconfigured root")
        return {"task_id": task.id, "action": "blocked",
                "reason": f"unknown workspace {spec.workspace_id!r}"}
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

    # resolve_effort only PROVES a mapping exists; applying it needs the
    # pinned Runner to actually carry the wire value. The operator attests
    # that with ``runner_effort_pin`` — any non-null resolved effort without
    # a matching pin fails closed rather than silently running another
    # effort than was approved.
    applied = {v for v in effort.values() if v is not None}
    if applied:
        pin = workspace.runner_effort_pin
        if pin is None or len(applied) != 1 or pin != next(iter(applied)):
            kernel.call(
                "block", task_id=task.id, kind="needs_input",
                reason=(
                    f"effort hint {spec.effort_hint.value!r} resolves to "
                    f"{sorted(applied)} on routed candidates but workspace "
                    f"{spec.workspace_id!r} has runner_effort_pin={pin!r} — "
                    "the pinned Runner cannot be proven to carry that "
                    "effort; refusing rather than running the wrong effort"
                ),
            )
            return {"task_id": task.id, "action": "blocked",
                    "reason": "effort_pin_missing: resolved effort "
                              f"{sorted(applied)} not pinned on workspace"}

    # 1. Stable reservation BEFORE any claim/HTTP side effect — an applied
    #    grant (when present) is consumed in the same transaction.
    try:
        res = store.reserve(
            task_id=task.id,
            task_revision=spec.task_revision,
            spec_hash=_spec_hash(spec),
            policy_fingerprint=fingerprint,
            workspace_id=spec.workspace_id,
            base_revision=spec.base_revision,
            route=decision.route,
            consume_approval=grant.approval_id if grant else None,
        )
    except ReservationExists:
        return {"task_id": task.id, "action": "skipped",
                "reason": "live_or_unknown_reservation_exists"}
    except GrantMismatch as exc:
        return {"task_id": task.id, "action": "needs_approval",
                "reason": str(exc)}

    try:
        return _execute_reserved(
            kernel, store, client, control_client, policy, task, spec,
            decision, res, workspace,
        )
    except KernelError as exc:
        # Kernel call failed mid-flow — we cannot prove what ran.
        _res, cancelled = _transition_guarded(
            store, control_client, None, res, task, None, UNKNOWN, {},
            detail=f"kernel error: {exc}",
        )
        if cancelled:
            return cancelled
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "unknown", "reason": str(exc)}


class _Heartbeat:
    """Real claim renewal during the HTTP+verification critical section —
    the kernel ``heartbeat`` op is actually called on an interval, not just
    promised. Daemon thread; stops with the section."""

    def __init__(self, kernel, task_id, claimer, ttl_seconds, interval):
        self._kernel = kernel
        self._task_id = task_id
        self._claimer = claimer
        self._ttl = ttl_seconds
        self._interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._kernel.call(
                    "heartbeat", task_id=self._task_id,
                    claimer=self._claimer, ttl_seconds=self._ttl,
                )
            except KernelError:
                return  # bridge dead — the main flow will surface it

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def _cancelled_path(kernel, store, client, res, task, krun, reason) -> dict:
    """Receipt may already be cancelled by the operator path — tolerate
    either order, always report cancelled."""
    if res.run_id:
        try:
            client.cancel_run(res.run_id)  # best effort; intent is durable
        except WrapperError:
            pass
    try:
        # A card already handed to review cannot be blocked directly —
        # reopen it to resumable state first, then block it so a cancelled
        # execution is never silently re-dispatched.
        cur = kernel.call("get_task", task_id=task.id).get("task")
        if cur and cur["status"] == "review":
            kernel.call("reopen_review", task_id=task.id)
            kernel.call("block", task_id=task.id, kind="needs_input",
                        reason=reason)
        else:
            kernel.call("block", task_id=task.id, kind="needs_input",
                        expected_run_id=krun, reason=reason)
    except KernelError:
        pass
    try:
        store.transition(res.dispatch_id, CANCELLED, detail=reason)
    except StoreError:
        pass  # already terminal (the cancel command beat us to it)
    return {"task_id": task.id, "dispatch_id": res.dispatch_id,
            "action": "cancelled", "run_id": res.run_id}


def _known_secret_values(policy: Policy) -> list[str]:
    """Credential-file contents used only to REDACT evidence — never sent,
    never logged."""
    out = []
    for attr in ("credential_file", "control_credential_file"):
        path = getattr(policy.execution, attr, None)
        if not path:
            continue
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            out.append(value)
    return out


def _execute_reserved(kernel, store, client, control_client, policy, task,
                      spec, decision, res, workspace) -> dict:
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
        try:
            store.transition(res.dispatch_id, ABORTED,
                             detail=f"dependencies not done: {undone}")
        except StoreError:
            return _cancelled_path(
                kernel, store, control_client,
                store.get(res.dispatch_id) or res, task, None,
                "cancelled before claim",
            )
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

    # 3. Workspace admission BEFORE the claim and BEFORE any HTTP: the
    #    submitted workspace id must resolve — through the protected Runner
    #    execution config — to the operator-prepared worktree pinned at this
    #    card's base revision. No binding, no wire.
    #    Direct mode submits the operator-declared preset alias; gateway mode
    #    submits the classifier route-derived combo name (``jev.<route>``) —
    #    a static model can never bypass classification.
    route = policy.routes[decision.route]
    model = (
        policy.execution.model
        if policy.execution.mode == "direct"
        else f"jev.{decision.route}"
    )
    submit_ws = workspace.wrapper_workspace_id or spec.workspace_id
    ephemeral = False
    try:
        if workspace.prepared_worktree:
            wt = validate_prepared_worktree(
                repo=repo,
                prepared=workspace.prepared_worktree,
                base_revision=spec.base_revision,
                workspace_id=spec.workspace_id,
            )
            validate_runner_binding(
                config_path=workspace.runner_execution_config,
                workspace_id=submit_ws,
                prepared_path=str(wt.path),
                submitted_model=model,
                candidate_models=_candidate_models(
                    policy, route, decision.capability
                ),
            )
        elif workspace.allow_ephemeral_worktree:
            wt = None  # created after the claim below
            ephemeral = True
        else:
            raise WorktreeError(
                f"workspace {spec.workspace_id!r} has no prepared worktree "
                "binding — refusing to submit an unbound execution root "
                "(set prepared_worktree + runner_execution_config, or the "
                "explicit development-only allow_ephemeral_worktree)"
            )
    except WorktreeError as exc:
        return _record_block(kernel, store, res, kind="needs_input",
                             reason=f"workspace admission failed: {exc}")

    # 4. max_retries=1 before claim — stock reclaim must never replay an
    #    external write.
    kernel.call("set_max_retries", task_id=task.id, value=1)

    # 5. Atomic claim.
    claimer = f"jev:{res.dispatch_id}"
    claimed = kernel.call(
        "claim", task_id=task.id,
        ttl_seconds=policy.dispatch.claim_ttl_seconds,
        claimer=claimer,
    )
    if not claimed["claimed"]:
        try:
            store.transition(res.dispatch_id, ABORTED,
                             detail="claim lost (claimed elsewhere or "
                                    "demoted)")
        except StoreError:
            return _cancelled_path(
                kernel, store, control_client,
                store.get(res.dispatch_id) or res, task, None,
                "cancelled before claim",
            )
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "skipped", "reason": "claim lost"}
    krun = (claimed["task"] or {}).get("current_run_id")

    if ephemeral:
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
    try:
        res = store.transition(res.dispatch_id, CLAIMED, kernel_run_id=krun,
                               branch=wt.branch, worktree=str(wt.path))
    except StoreError:
        return _cancelled_path(
            kernel, store, control_client,
            store.get(res.dispatch_id) or res, task, krun,
            "cancelled during claim",
        )

    # 6. Cancel intent recorded before submit wins — the execution would be
    #    unowned the moment it landed.
    res = store.get(res.dispatch_id) or res
    if res.cancel_requested:
        return _cancelled_path(
            kernel, store, control_client, res, task, krun,
            "cancel requested before submit",
        )

    # 7. Submit the single model request — full JSON body, never first byte.
    #    The claim is renewed for real for the whole HTTP+verification
    #    critical section (heartbeat thread, not a documented aspiration).
    execution_meta = None
    if policy.dispatch.send_execution_metadata:
        execution_meta = {
            "task_revision": spec.task_revision,
            "base_revision": spec.base_revision,
            "route": decision.route,
            "policy_version": policy.policy_version,
        }
    heartbeat = _Heartbeat(
        kernel, task.id, claimer,
        policy.dispatch.claim_ttl_seconds, policy.dispatch.heartbeat_seconds,
    )
    heartbeat.start()
    try:
        try:
            outcome = client.submit_chat(
                model=model,
                task_id=task.id,
                workspace_id=submit_ws,
                messages=[{"role": "user", "content": _prompt(spec)}],
                execution=execution_meta,
            )
        except WrapperTransportError as exc:
            # Unconfirmed transport: the request may have reached the wrapper.
            kernel.call("block", task_id=task.id, kind="needs_input",
                        expected_run_id=krun,
                        reason=f"transport unconfirmed — run state unknown: "
                               f"{exc}")
            _res, cancelled = _transition_guarded(
                store, control_client, kernel, res, task, krun, UNKNOWN, {},
                detail=str(exc),
            )
            if cancelled:
                return cancelled
            return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                    "action": "unknown", "reason": str(exc)}
        except WrapperHTTPError as exc:
            run = exc.run
            if run and run.get("run_id"):
                # The wrapper rejected AFTER attaching a run — reconcile.
                try:
                    res = store.transition(
                        res.dispatch_id, SUBMITTED, run_id=run["run_id"],
                        attempt_id=run.get("attempt_id"),
                    )
                except StoreError:
                    return _cancelled_path(
                        kernel, store, control_client,
                        store.get(res.dispatch_id), task, krun,
                        "cancelled while submit was being reconciled",
                    )
                return _handle_run_status(
                    kernel, store, control_client, policy, task, spec, res,
                    wt, run, claimer, krun, execution_meta, submit_ws,
                    ephemeral=ephemeral,
                )
            # Rejected before any run existed (auth/validation): no execution.
            return _record_block(kernel, store, res, kind="needs_input",
                                 reason=f"wrapper rejected the request: {exc}")
        try:
            res = store.transition(res.dispatch_id, SUBMITTED,
                                   run_id=outcome.run_id,
                                   attempt_id=outcome.attempt_id)
        except StoreError:
            return _cancelled_path(
                kernel, store, control_client, store.get(res.dispatch_id),
                task, krun, "cancelled while submit was being reconciled",
            )
        return _handle_run_status(
            kernel, store, control_client, policy, task, spec, res, wt,
            outcome.run, claimer, krun, execution_meta, submit_ws,
            ephemeral=ephemeral,
        )
    finally:
        heartbeat.stop()


def _transition_guarded(store, control_client, kernel, res, task, krun,
                        state, result_fields, **kw):
    """Transition that loses to a persisted cancel: ``request_cancel`` can
    land between any two steps, so a StoreError here means the operator
    path already closed the receipt — return the cancelled result instead
    of crashing the tick."""
    try:
        return store.transition(res.dispatch_id, state, **kw), None
    except StoreError:
        cur = store.get(res.dispatch_id)
        if cur is not None and (cur.cancel_requested or cur.state == CANCELLED):
            return None, _cancelled_path(
                kernel, store, control_client, cur, task, krun,
                "cancelled during status handling",
            )
        raise


def _handle_run_status(kernel, store, control_client, policy, task, spec,
                       res, wt, run, claimer, krun, execution_meta,
                       submit_ws, *, ephemeral) -> dict:
    """Canonical run view -> verify -> review (or a typed hold/block).

    Only ``status == "completed"`` AND ``outcome == "succeeded"`` may enter
    verification — and only when the run's task/workspace/execution context
    equals the reserved context. A live or unrecognized status is
    ``in_flight``: the card is parked needs_input and the receipt stays
    submitted (blocking blind re-dispatch) until an operator resolves it.
    """
    status = str(run.get("status") or "unknown")
    outcome = run.get("outcome")

    # Canonical context must equal what we reserved — a foreign run view is
    # anomalous, never verified, never reviewed.
    anomalies = []
    if run.get("task_id") != task.id:
        anomalies.append(
            f"task_id {run.get('task_id')!r} != reserved {task.id!r}"
        )
    if run.get("workspace_id") != submit_ws:
        anomalies.append(
            f"workspace_id {run.get('workspace_id')!r} != submitted "
            f"{submit_ws!r}"
        )
    if execution_meta is not None and run.get("execution") != execution_meta:
        anomalies.append("execution metadata echo differs from what was sent")
    if anomalies:
        reason = "run context mismatch — refusing to verify a foreign run: " \
            + "; ".join(anomalies)
        kernel.call("block", task_id=task.id, kind="needs_input",
                    expected_run_id=krun, reason=reason)
        _res, cancelled = _transition_guarded(
            store, control_client, kernel, res, task, krun, UNKNOWN, {},
            detail=reason,
        )
        if cancelled:
            return cancelled
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "unknown", "run_id": res.run_id,
                "reason": reason}

    if status == "cancelled":
        kernel.call("block", task_id=task.id, kind="needs_input",
                    expected_run_id=krun,
                    reason=f"wrapper run {res.run_id} cancelled")
        _res, cancelled = _transition_guarded(
            store, control_client, kernel, res, task, krun, CANCELLED, {},
            detail="run cancelled",
        )
        if cancelled:
            return cancelled
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "cancelled", "run_id": res.run_id}
    if status == "failed":
        kernel.call("block", task_id=task.id, kind="needs_input",
                    expected_run_id=krun,
                    reason=f"wrapper run {res.run_id} failed: "
                           f"{(run.get('detail') or '')[:200]}")
        _res, cancelled = _transition_guarded(
            store, control_client, kernel, res, task, krun, FAILED, {},
            detail="run failed",
        )
        if cancelled:
            return cancelled
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "failed", "run_id": res.run_id}
    if status != "completed":
        # Live or unrecognized — hold. The receipt stays submitted (live),
        # which permanently blocks blind re-dispatch until `control resolve`.
        kernel.call("block", task_id=task.id, kind="needs_input",
                    expected_run_id=krun,
                    reason=f"wrapper run {res.run_id} still in flight "
                           f"(status={status!r}) — not a terminal success; "
                           "resolve via control once it settles")
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "in_flight", "run_id": res.run_id,
                "dispatched": True}
    if outcome != "succeeded":
        # completed without a succeeded outcome is a known-terminal failure,
        # never a success candidate.
        reason = (f"wrapper run {res.run_id} completed with outcome "
                  f"{outcome!r} (need 'succeeded') — not promotable")
        kernel.call("block", task_id=task.id, kind="needs_input",
                    expected_run_id=krun, reason=reason)
        _res, cancelled = _transition_guarded(
            store, control_client, kernel, res, task, krun, FAILED, {},
            detail=reason,
        )
        if cancelled:
            return cancelled
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "blocked", "run_id": res.run_id,
                "reason": f"quality_failed: {reason}", "dispatched": True}

    # Cancel guard BEFORE verification work — atomic against request_cancel.
    try:
        res = store.transition(res.dispatch_id, COMPLETING,
                               require_no_cancel=True)
    except StoreError:
        return _cancelled_path(
            kernel, store, control_client, store.get(res.dispatch_id), task,
            krun, "cancel requested before verification",
        )

    # Verification: trusted full argv only, bounded capture, committed+dirty
    # diff, declared-scope enforcement, durable sanitized evidence.
    secrets = _known_secret_values(policy)
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

    evidence_dir = Path(store.path).resolve().parent / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    diff_bytes = diff.encode("utf-8", "replace")
    diff_path = evidence_dir / f"{res.dispatch_id}.diff"
    diff_path.write_bytes(diff_bytes)
    artifact_entries = []
    artifact_hash_failed = False
    for artifact in artifacts:
        try:
            digest, size = sha256_file(
                artifact, max_bytes=policy.limits.max_spec_bytes
            )
        except WorktreeError:
            artifact_hash_failed = True
            continue
        artifact_entries.append(
            {"path": str(artifact), "sha256": digest, "bytes": size}
        )
    evidence = {
        "verification": {
            "argv": list(spec.verification.argv),
            "exit_code": verify.exit_code,
            "output": sanitize_text(verify.output, secrets)[-4096:],
            "truncated": verify.truncated,
            "timed_out": verify.timed_out,
        },
        "artifacts": artifact_entries,
        "diff": {
            "path": str(diff_path),
            "sha256": sha256_bytes(diff_bytes),
            "bytes": len(diff_bytes),
        },
        "changed_files": changed,
        "scope_violations": violations,
        "base_revision": spec.base_revision,
        "policy_fingerprint": res.policy_fingerprint,
    }
    store.record_evidence(res.dispatch_id, evidence)

    if not verify.ok or missing or violations or artifact_hash_failed:
        reasons = []
        if not verify.ok:
            reasons.append(
                f"verification failed (exit={verify.exit_code}): "
                f"{sanitize_text(verify.output, secrets)[-400:]}"
            )
        if missing:
            reasons.append(f"missing artifacts: {missing}")
        if artifact_hash_failed:
            reasons.append("an artifact exceeded the hash size bound")
        if violations:
            reasons.append(
                f"changed files outside allowed_scope: {violations}"
            )
        reason = "quality_failed: " + "; ".join(reasons)
        kernel.call("block", task_id=task.id, kind="needs_input",
                    expected_run_id=krun, reason=reason)
        _res, cancelled = _transition_guarded(
            store, control_client, kernel, res, task, krun, BLOCKED, {},
            detail=reason,
        )
        if cancelled:
            return cancelled
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "blocked", "reason": reason,
                "dispatched": True}

    # Handoff to review under the expected-run fence; the receipt transition
    # is guarded atomically so a persisted cancel can never be promoted.
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
            "diff_path": str(diff_path),
            "diff_sha256": sha256_bytes(diff_bytes),
            "diff_bytes": len(diff_bytes),
        },
        expected_run_id=krun,
    )
    if not resp["ok"]:
        # Fence lost or kernel refused — do NOT guess; unknown, never replay.
        _res, cancelled = _transition_guarded(
            store, control_client, kernel, res, task, krun, UNKNOWN, {},
            detail=f"request_review refused: {resp.get('reason')}",
        )
        if cancelled:
            return cancelled
        return {"task_id": task.id, "dispatch_id": res.dispatch_id,
                "action": "unknown",
                "reason": f"review handoff refused: {resp.get('reason')}",
                "dispatched": True}
    try:
        store.transition(res.dispatch_id, REVIEW, require_no_cancel=True,
                         detail=f"run {res.run_id} verified; awaiting Lead "
                                "review")
    except StoreError:
        # The cancel intent landed between the kernel handoff and the
        # receipt guard — the card sits in review but the run is cancelled;
        # _cancelled_path pulls it back honestly (review -> resumable ->
        # blocked) rather than leave a cancelled execution looking
        # promotable.
        return _cancelled_path(
            kernel, store, control_client, store.get(res.dispatch_id), task,
            krun,
            "cancelled during review handoff — card pulled back from review",
        )
    if policy.approval.notify is not None:
        kernel.call(
            "notify_sub", task_id=task.id,
            platform=policy.approval.notify.platform,
            chat_id=policy.approval.notify.chat_id,
            thread_id=policy.approval.notify.thread_id,
            user_id=policy.approval.notify.user_id,
        )
    result = {"task_id": task.id, "dispatch_id": res.dispatch_id,
              "action": "review", "run_id": res.run_id, "dispatched": True}
    if ephemeral:
        # Explicit development lane — the run executed against an
        # autogenerated worktree, not an operator-bound one. Labeled, never
        # presented as the production path.
        result["synthetic"] = True
        result["ephemeral"] = True
    return result
