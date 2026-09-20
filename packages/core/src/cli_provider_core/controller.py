"""RunController: lifecycle, idempotency, cancellation, quarantine, artifacts.

Reserve+commit happens before any Runner dispatch. Runner RPC ``ok:true`` is
never treated as run success: only a validated terminal event decides
status/outcome. Unknown outcomes quarantine the Runner instance so released
capacity cannot be reused while old work may still be running.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from .config import OperatorConfig, PresetConfig
from .errors import (
    Conflict,
    NotFound,
    QueueFull,
    QueueTimeout,
    RunnerQuarantined,
    RunnerRunRejected,
    RunnerUnavailable,
    UpstreamProtocolError,
)
from .hashing import request_hash
from .ids import new_artifact_id, new_attempt_id, new_run_id
from .models import (
    ACTIVE_STATUSES,
    CANCELLED,
    CANCELLING,
    COMPLETED,
    FAILED,
    OUTCOME_CANCELLED,
    OUTCOME_PARTIAL,
    OUTCOME_PROVIDER_ERROR,
    OUTCOME_QUEUE_TIMEOUT,
    OUTCOME_REJECTED,
    OUTCOME_SUCCEEDED,
    OUTCOME_UNKNOWN,
    QUEUED,
    RESERVED,
    RUNNING,
    STARTING,
    TERMINAL_STATUSES,
    UNKNOWN,
    ArtifactRecord,
    AttemptRecord,
    EventRecord,
)
from .registry import RunnerRegistry
from .store import Store, proven_pre_execution, utcnow

_ACTIVE = sorted(ACTIVE_STATUSES)
# Terminals that are a validated driver outcome (unlike `unknown`, which is a
# placeholder for unresolved execution and never reconciles a quarantine).
_VALIDATED_TERMINALS = frozenset({COMPLETED, FAILED, CANCELLED})

# Finite local scheduling/framing margin added on top of the Runner's declared
# cleanup budget. It is a fixed bound (never derived from request input and
# never unbounded); the substantive bound comes from the validated Runner value.
_SCHEDULING_MARGIN_SECONDS = 1.0


@dataclass
class ActiveRun:
    record: AttemptRecord
    messages: list[dict[str, Any]]
    # Event fan-out is non-blocking: every accepted event is appended durably to
    # the store first, then this wakeup is set. Readers replay from the store, so
    # slow or absent subscribers can never block the run's own progress and a
    # dropped notification cannot lose an event.
    wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_requested: bool = False
    terminal_status: str | None = None
    last_event_sequence: int = 0
    task: asyncio.Task | None = None


@dataclass
class Submission:
    cached: bool
    record: AttemptRecord
    active: ActiveRun | None = None


@dataclass
class CancelView:
    run_id: str
    status: str
    requested: bool
    confirmed: bool
    detail: str


class RunController:
    def __init__(
        self,
        *,
        config: OperatorConfig,
        store: Store,
        registry: RunnerRegistry,
    ) -> None:
        self._config = config
        self._store = store
        self._registry = registry
        self._active: dict[str, ActiveRun] = {}
        self._runner_sems: dict[str, asyncio.Semaphore] = {}
        self._principal_sems: dict[str, asyncio.Semaphore] = {}
        # Bounded admission counters: outstanding = reserved/queued + running.
        self._runner_outstanding: dict[str, int] = {}
        self._principal_outstanding: dict[str, int] = {}
        self._lock = asyncio.Lock()
        os.makedirs(self._config.artifacts_dir(), mode=0o700, exist_ok=True)

    # --------------------------------------------------------------- submit

    def _runner_sem(self, instance_id: str) -> asyncio.Semaphore:
        sem = self._runner_sems.get(instance_id)
        if sem is None:
            # Never dispatch more concurrent runs than the Runner has verified it
            # can execute. The Runner serialises at one slot, so configured
            # per_runner > capacity would otherwise push the excess into the
            # Runner's opaque queue (unbounded from the API's perspective) and a
            # healthy queued run could be misread as `unknown`.
            configured = self._config.api.concurrency.per_runner
            capacity = self._registry.runner_capacity(instance_id)
            sem = asyncio.Semaphore(max(1, min(configured, capacity)))
            self._runner_sems[instance_id] = sem
        return sem

    def run_budget(self, instance_id: str, deadline_seconds: float) -> float:
        """Finite outer stream budget for the whole run RPC.

        Derived from the Runner's *validated* cancellation cleanup budget rather
        than this API's own ``cancel_deadline_seconds``: a mismatched operator
        config must never cut the Runner's unwind short and turn a healthy run
        into a false ``unknown``.
        """
        return (
            deadline_seconds
            + self._registry.runner_cleanup_seconds(instance_id)
            + _SCHEDULING_MARGIN_SECONDS
        )

    def _principal_sem(self, principal: str, limit: int) -> asyncio.Semaphore:
        sem = self._principal_sems.get(principal)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            self._principal_sems[principal] = sem
        return sem

    async def submit(
        self,
        *,
        principal: str,
        principal_concurrency: int,
        task_id: str,
        preset: PresetConfig,
        workspace_id: str,
        messages: Sequence[Mapping[str, Any]],
        deadline_seconds: float,
        execution: Mapping[str, Any] | None = None,
    ) -> Submission:
        instance_id = preset.runner_ref
        # Operator config owns the task policy: it is never caller-selected.
        task_policy = preset.task_policy
        digest = request_hash(
            principal=principal,
            task_id=task_id,
            workspace_id=workspace_id,
            task_policy=task_policy,
            messages=messages,
            execution=execution,
        )

        existing = self._store.latest_attempt(principal, task_id)
        if existing is not None:
            task = self._store.get_task(principal, task_id)
            if task is not None and task["request_hash"] != digest:
                raise Conflict(
                    "task_id already exists with different content",
                    code="task_content_conflict",
                )
            if existing.status in _ACTIVE:
                raise Conflict(
                    "an identical or overlapping attempt is already active",
                    code="run_active",
                    run_id=existing.run_id,
                )
            if existing.status == UNKNOWN:
                raise Conflict(
                    "a previous attempt is unknown and will not be retried",
                    code="unknown_attempt",
                    run_id=existing.run_id,
                )
            if existing.status == COMPLETED:
                if existing.preset != preset.alias:
                    raise Conflict(
                        "task already completed under a different model; a cached "
                        "result is not relabelled as a new-model inference",
                        code="model_conflict",
                        run_id=existing.run_id,
                    )
                return Submission(cached=True, record=replace(existing, cached=True))
            # Any other terminal attempt may already have had Runner-side
            # effects; a new attempt is admitted only when every prior attempt
            # is proven pre-execution. Store.reserve re-checks the same rule
            # inside the reservation transaction, so concurrent admissions
            # cannot race past it.
            if not proven_pre_execution(
                existing.status, existing.outcome, existing.started_at
            ):
                raise Conflict(
                    "a previous attempt for this task already reached or "
                    "completed execution; automatic retry is never admitted",
                    code="run_not_retryable",
                    run_id=existing.run_id,
                )

        # Only a *new* execution needs a verified, non-quarantined runner.
        if not self._registry.runner_available(instance_id):
            raise RunnerUnavailable(f"runner {instance_id!r} is not verified/available")
        if self._store.get_quarantine(instance_id) is not None:
            raise RunnerQuarantined(
                f"runner {instance_id!r} is quarantined after an unconfirmed execution"
            )

        # Bounded admission, checked atomically (no await) before anything is
        # allocated or dispatched. Excess work is refused with a structured 429
        # and has no Runner effect at all.
        concurrency = self._config.api.concurrency
        runner_limit = concurrency.per_runner + concurrency.max_queued_per_runner
        # The global per-principal cap is a real bound: the effective concurrency
        # is the smaller of it and the principal's own max_concurrency. Both the
        # admission accounting and the semaphore use the same clamped value so
        # the global setting is never dead.
        effective_principal = min(concurrency.per_principal, principal_concurrency)
        principal_limit = effective_principal + concurrency.max_queued_per_principal
        if self._runner_outstanding.get(instance_id, 0) >= runner_limit:
            raise QueueFull(
                f"runner {instance_id!r} is at its outstanding-run bound "
                f"({runner_limit})"
            )
        if self._principal_outstanding.get(principal, 0) >= principal_limit:
            raise QueueFull(
                f"principal {principal!r} is at its outstanding-run bound "
                f"({principal_limit})"
            )

        run_id = new_run_id()
        attempt_id = new_attempt_id()
        normalised_messages = [
            {"role": str(m.get("role")), "content": str(m.get("content"))} for m in messages
        ]
        try:
            record = self._store.reserve(
                run_id=run_id,
                attempt_id=attempt_id,
                principal=principal,
                task_id=task_id,
                preset=preset.alias,
                driver_id=self._registry.runner_config(instance_id).driver_id,
                runner_instance=instance_id,
                workspace_id=workspace_id,
                request_hash=digest,
                task_policy=task_policy,
                status=QUEUED,
                synthetic=self._registry.runner_synthetic(instance_id),
                execution=dict(execution) if execution is not None else None,
            )
        except Conflict as exc:
            if exc.code != "run_not_retryable":
                raise
            # A completion raced the pre-check above: an identical replay under
            # the same preset still gets the cached result, never a second
            # execution and never a relabelled model.
            latest = self._store.latest_attempt(principal, task_id)
            task = self._store.get_task(principal, task_id)
            if (
                latest is None
                or latest.status != COMPLETED
                or task is None
                or task["request_hash"] != digest
            ):
                raise
            if latest.preset != preset.alias:
                raise Conflict(
                    "task already completed under a different model; a cached "
                    "result is not relabelled as a new-model inference",
                    code="model_conflict",
                    run_id=latest.run_id,
                ) from exc
            return Submission(cached=True, record=replace(latest, cached=True))
        self._runner_outstanding[instance_id] = (
            self._runner_outstanding.get(instance_id, 0) + 1
        )
        self._principal_outstanding[principal] = (
            self._principal_outstanding.get(principal, 0) + 1
        )
        active = ActiveRun(record=record, messages=normalised_messages)
        active.task = asyncio.create_task(
            self._execute(active, effective_principal, deadline_seconds, preset)
        )
        self._active[run_id] = active
        return Submission(cached=False, record=record, active=active)

    def _release_outstanding(self, instance_id: str, principal: str) -> None:
        for counter, key in (
            (self._runner_outstanding, instance_id),
            (self._principal_outstanding, principal),
        ):
            remaining = counter.get(key, 0) - 1
            if remaining > 0:
                counter[key] = remaining
            else:
                counter.pop(key, None)

    # -------------------------------------------------------------- execute

    async def _acquire_slots(
        self, instance_id: str, principal: str, limit: int, timeout: float
    ) -> None:
        runner_sem = self._runner_sem(instance_id)
        principal_sem = self._principal_sem(principal, limit)
        acquired_runner = False
        try:
            await asyncio.wait_for(runner_sem.acquire(), timeout=timeout)
            acquired_runner = True
            await asyncio.wait_for(principal_sem.acquire(), timeout=timeout)
        except BaseException:
            if acquired_runner:
                runner_sem.release()
            raise

    async def _execute(
        self,
        active: ActiveRun,
        principal_concurrency: int,
        deadline_seconds: float,
        preset: PresetConfig,
    ) -> AttemptRecord:
        record = active.record
        run_id = record.run_id
        instance_id = record.runner_instance
        store = self._store
        timeout = self._config.api.concurrency.queue_timeout_seconds
        acquire = asyncio.create_task(
            self._acquire_slots(instance_id, record.principal, principal_concurrency, timeout)
        )
        cancel_wait = asyncio.create_task(active.cancel_event.wait())
        acquired = False
        try:
            done, _ = await asyncio.wait(
                {acquire, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if acquire in done:
                if not cancel_wait.done():
                    cancel_wait.cancel()
                error = acquire.exception()
                if error is not None:
                    if isinstance(error, asyncio.TimeoutError):
                        self._finish(
                            run_id,
                            status=FAILED,
                            outcome=OUTCOME_QUEUE_TIMEOUT,
                            detail="queue timeout before dispatch",
                        )
                    else:
                        self._finish(
                            run_id,
                            status=FAILED,
                            outcome=OUTCOME_REJECTED,
                            detail="dispatch rejected before execution",
                        )
                    return self._require(run_id)
                acquired = True
            else:
                acquire.cancel()
                # Cancelled while queued: the driver is never started.
                self._finish(
                    run_id,
                    status=CANCELLED,
                    outcome=OUTCOME_CANCELLED,
                    detail="cancelled before driver start; never executed",
                )
                return self._require(run_id)

            if self._store.get_quarantine(instance_id) is not None:
                self._finish(
                    run_id,
                    status=FAILED,
                    outcome=OUTCOME_REJECTED,
                    detail="runner quarantined before dispatch",
                )
                return self._require(run_id)

            self._mark_starting(run_id)
            session = self._registry.session(instance_id)
            # The outer wait_for is the hard, finite bound for the whole stream,
            # derived from the Runner's declared cleanup budget.
            budget = self.run_budget(instance_id, deadline_seconds)
            # The streaming frame timeout follows the run's actual deadline
            # budget (never a fixed 15 s). An explicit operator override is
            # honored, but only up to that finite bound: it may tighten the
            # per-frame wait, never make it unbounded or exceed the deadline
            # contract. Control RPCs keep their own short timeout.
            override = getattr(session, "run_timeout_seconds", None)
            effective_timeout = (
                budget if override is None else min(float(override), budget)
            )
            set_run_timeout = getattr(session, "set_run_timeout", None)
            if callable(set_run_timeout):
                set_run_timeout(effective_timeout)
            try:
                result = await asyncio.wait_for(
                    self._stream(active, session, deadline_seconds, preset),
                    timeout=budget,
                )
            except asyncio.TimeoutError as exc:
                raise UpstreamProtocolError(
                    "run exceeded the bounded runner budget"
                ) from exc
            finally:
                await session.aclose()
            return result
        except asyncio.CancelledError:
            self._finish(
                run_id,
                status=UNKNOWN,
                outcome=OUTCOME_UNKNOWN,
                detail="run task cancelled",
            )
            self._quarantine(instance_id, run_id, "run task cancelled")
            raise
        except RunnerRunRejected as exc:
            if exc.no_effect:
                # The Runner provably rejected this run before starting the
                # driver (e.g. QUEUE_FULL / INVALID_PARAMS): nothing executed,
                # so it is a rejection, never an unknown execution, and the
                # Runner stays healthy.
                self._finish(
                    run_id,
                    status=FAILED,
                    outcome=OUTCOME_REJECTED,
                    detail=f"runner rejected the run before execution: {exc.runner_code}",
                )
            else:
                # The error surfaced after execution may have begun, so the
                # effect is not provably absent: fail closed.
                self._finish(
                    run_id,
                    status=UNKNOWN,
                    outcome=OUTCOME_UNKNOWN,
                    detail=f"run failed after execution began: {exc.runner_code}",
                )
                self._quarantine(instance_id, run_id, f"runner {exc.runner_code}")
            return self._require(run_id)
        except Exception as exc:  # noqa: BLE001 - converted to safe classification
            self._finish(
                run_id,
                status=UNKNOWN,
                outcome=OUTCOME_UNKNOWN,
                detail=f"run failed: {type(exc).__name__}",
            )
            self._quarantine(instance_id, run_id, f"{type(exc).__name__}")
            return self._require(run_id)
        finally:
            if not cancel_wait.done():
                cancel_wait.cancel()
            if acquired:
                self._runner_sem(instance_id).release()
                self._principal_sem(
                    record.principal, principal_concurrency
                ).release()
            self._active.pop(run_id, None)
            self._release_outstanding(instance_id, record.principal)
            if active.terminal_status is None:
                active.terminal_status = self._require(run_id).status
            # Wake any subscriber so it can observe the durable terminal state
            # and stop; end detection never depends on a queue sentinel.
            active.wakeup.set()

    async def _stream(
        self,
        active: ActiveRun,
        session: Any,
        deadline_seconds: float,
        preset: PresetConfig,
    ) -> AttemptRecord:
        record = active.record
        run_id = record.run_id
        limits = self._config.api.limits
        params: dict[str, Any] = {
            "run_id": run_id,
            "task_id": record.task_id,
            "attempt_id": record.attempt_id,
            "preset": preset.alias,
            "workspace": {"workspace_id": record.workspace_id},
            "messages": active.messages,
            "deadline_seconds": deadline_seconds,
        }
        if record.execution is not None:
            # Authenticated dispatcher context; opaque to the runtime, carried
            # verbatim to the worker. Runner protocol support for the field is
            # required for dispatch — a Runner without it rejects pre-execution.
            params["execution"] = dict(record.execution)
        chunks: list[str] = []
        output_bytes = 0
        events_seen = 0
        first = True
        async for event in session.run(params):
            events_seen += 1
            if events_seen > limits.max_events_per_run:
                raise UpstreamProtocolError("runner exceeded the event budget")
            self._store.append_event(run_id, event)
            active.last_event_sequence = int(
                event.get("sequence", active.last_event_sequence)
            )
            if first:
                # Monotonic status: a cancel may already have moved this run to
                # CANCELLING, and the first event must never regress it back to
                # RUNNING. Only a pre-execution state may advance to RUNNING.
                current = self._require(run_id).status
                if current in (STARTING, RUNNING):
                    self._store.set_attempt(run_id, status=RUNNING)
                first = False
            if event.get("kind") == "message.delta":
                text = str(event.get("payload", {}).get("text", ""))
                output_bytes += len(text.encode("utf-8"))
                if output_bytes > limits.max_output_bytes:
                    raise UpstreamProtocolError("runner exceeded the output budget")
                chunks.append(text)
            # Non-blocking fan-out: the event is already durable, so a slow or
            # absent subscriber can never stall the run here.
            active.wakeup.set()

        result = session.last_result
        if result is None:
            raise UpstreamProtocolError("runner produced no terminal result")

        summary = "".join(chunks) if chunks else None
        status = str(result.status.value)
        outcome = str(result.outcome.value)
        detail = result.detail
        if status == UNKNOWN:
            self._finish(
                run_id,
                status=UNKNOWN,
                outcome=OUTCOME_UNKNOWN,
                detail=detail or "runner reported an unknown terminal",
                verification=_verification(result),
                usage=_usage(result),
                summary=summary,
            )
            self._quarantine(record.runner_instance, run_id, "unknown terminal")
            return self._require(run_id)

        verification: dict[str, Any] = _verification(result)
        usage: dict[str, Any] = _usage(result)
        self._finish(
            run_id,
            status=status,
            outcome=outcome,
            detail=detail,
            verification=verification,
            usage=usage,
            summary=summary,
        )
        if status == COMPLETED and summary:
            self._persist_artifact(record, summary)
        return self._require(run_id)

    def _finish(
        self,
        run_id: str,
        *,
        status: str,
        outcome: str | None,
        detail: str | None = None,
        verification: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        summary: str | None = None,
    ) -> None:
        self._store.set_attempt(
            run_id,
            status=status,
            outcome=outcome,
            detail=detail,
            verification=verification,
            usage=usage,
            summary=summary,
            finished_at=utcnow(),
        )
        active = self._active.get(run_id)
        if active is not None:
            active.terminal_status = status
            active.wakeup.set()
        if status in _VALIDATED_TERMINALS:
            # A later validated terminal proves the outcome of the run that
            # caused an uncertain-cancel quarantine, so reconcile it instead of
            # leaving a completed run paired with a quarantined Runner.
            self._reconcile_quarantine(run_id)

    def _reconcile_quarantine(self, run_id: str) -> None:
        record = self._store.get_attempt(run_id)
        if record is None:
            return
        quarantine = self._store.get_quarantine(record.runner_instance)
        if quarantine is not None and quarantine.get("run_id") == run_id:
            self._store.clear_quarantine(record.runner_instance)

    def _quarantine(self, instance_id: str, run_id: str, reason: str) -> None:
        self._store.quarantine_instance(instance_id, reason, run_id)

    def _require(self, run_id: str) -> AttemptRecord:
        record = self._store.get_attempt(run_id)
        assert record is not None
        return record

    def _mark_starting(self, run_id: str) -> None:
        """Advance to STARTING only from a pre-start state.

        A cancel that lands in the queued→starting window sets CANCELLING; this
        dispatch must never regress that back to STARTING (and then RUNNING).
        """
        current = self._require(run_id).status
        if current in (QUEUED, RESERVED, STARTING):
            self._store.set_attempt(run_id, status=STARTING, started_at=utcnow())
        else:
            self._store.set_attempt(run_id, started_at=utcnow())

    # ------------------------------------------------------------- artifacts

    def _persist_artifact(self, record: AttemptRecord, text: str) -> None:
        """Persist the result artifact without risking the validated terminal.

        Artifact bytes are best-effort output, not the run outcome. The terminal
        status/outcome is already durably stored and must never be overwritten to
        `unknown` (nor the Runner quarantined) by a storage failure here. The
        failure is recorded as a bounded, path-free limitation on the attempt so
        the response cannot claim an artifact that does not exist.
        """
        try:
            self._write_artifact(record, text)
        except Exception as exc:  # noqa: BLE001 - non-fatal, recorded safely
            current = self._require(record.run_id)
            note = (
                "run completed but the result artifact was not persisted "
                f"({type(exc).__name__})"
            )
            self._store.set_attempt(
                record.run_id,
                detail=f"{current.detail}; {note}" if current.detail else note,
            )

    def _write_artifact(self, record: AttemptRecord, text: str) -> ArtifactRecord | None:
        data = text.encode("utf-8")
        if len(data) > self._config.api.limits.max_artifact_bytes:
            return None
        artifact_id = new_artifact_id()
        path = os.path.join(self._config.artifacts_dir(), f"{artifact_id}.txt")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return None
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            artifact = ArtifactRecord(
                artifact_id=artifact_id,
                run_id=record.run_id,
                principal=record.principal,
                preset=record.preset,
                workspace_id=record.workspace_id,
                kind="text",
                content_type="text/plain; charset=utf-8",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                path=path,
                created_at=utcnow(),
            )
            self._store.add_artifact(artifact)
        except BaseException:
            # Never leave an untracked (possibly partial) file for a failed write.
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return artifact

    # ---------------------------------------------------------------- cancel

    async def cancel(self, run_id: str, principal: str) -> CancelView:
        record = self._store.get_attempt(run_id)
        if record is None or record.principal != principal:
            raise NotFound("run not found")
        active = self._active.get(run_id)

        if record.status in (QUEUED, RESERVED):
            if active is None:
                self._finish(
                    run_id,
                    status=CANCELLED,
                    outcome=OUTCOME_CANCELLED,
                    detail="cancelled while queued before dispatch",
                )
                return CancelView(run_id, CANCELLED, True, True, "never executed")
            active.cancel_requested = True
            active.cancel_event.set()
            # Wait without cancelling the run task: cancelling it here would turn
            # a legitimate queued cancel into an unknown/quarantined run.
            done, _ = await asyncio.wait(
                {active.task}, timeout=self._config.api.cancel_deadline_seconds
            )
            if active.task not in done:
                # The cancel may have raced the queued->starting transition (the
                # pre-start flag is no longer watched). Re-read the durable state
                # and, if dispatch has begun, issue a real driver cancel instead
                # of silently dropping the request.
                current = self._require(run_id)
                if current.status in (STARTING, RUNNING, CANCELLING):
                    return await self._cancel_dispatched(current, active)
                if current.status in TERMINAL_STATUSES:
                    return CancelView(
                        run_id,
                        current.status,
                        True,
                        current.status == CANCELLED,
                        "run reached a terminal state",
                    )
                return CancelView(
                    run_id,
                    current.status,
                    True,
                    False,
                    "queued cancellation not confirmed",
                )
            final = self._require(run_id)
            confirmed = final.status == CANCELLED
            return CancelView(
                run_id,
                final.status,
                True,
                confirmed,
                "cancelled before driver start" if confirmed else "not confirmed",
            )

        if record.status in (STARTING, RUNNING, CANCELLING):
            return await self._cancel_dispatched(record, active)

        return CancelView(
            run_id,
            record.status,
            False,
            record.status == CANCELLED,
            "run already reached a terminal state",
        )

    async def _cancel_dispatched(
        self, record: AttemptRecord, active: ActiveRun | None
    ) -> CancelView:
        """Issue a bounded driver cancel and re-evaluate the durable state."""
        run_id = record.run_id
        if active is not None:
            active.cancel_requested = True
        self._store.set_attempt(run_id, status=CANCELLING)
        session = self._registry.session(record.runner_instance)
        detail = "cancel requested"
        try:
            driver_result = await asyncio.wait_for(
                session.cancel(run_id),
                timeout=self._config.api.cancel_deadline_seconds,
            )
            detail = driver_result.detail or detail
        except Exception as exc:  # noqa: BLE001
            detail = f"cancel call failed: {type(exc).__name__}"
        finally:
            await session.aclose()

        if active is not None:
            # Do not cancel the run task on timeout; just stop waiting for it.
            await asyncio.wait(
                {active.task}, timeout=self._config.api.cancel_deadline_seconds
            )

        final = self._require(run_id)
        if final.status in TERMINAL_STATUSES:
            # A validated terminal (completed/failed/cancelled) is authoritative:
            # a late cancel never downgrades it or quarantines the Runner.
            return CancelView(
                run_id, final.status, True, final.status == CANCELLED, detail
            )

        # Execution genuinely remains unresolved within the cancel budget.
        self._finish(
            run_id,
            status=UNKNOWN,
            outcome=OUTCOME_UNKNOWN,
            detail="cancellation not confirmed; execution outcome unknown",
        )
        self._quarantine(
            record.runner_instance, run_id, "cancellation not confirmed"
        )
        final = self._require(run_id)
        return CancelView(run_id, final.status, True, False, detail)

    # ----------------------------------------------------------------- reads

    def owned_attempt(self, run_id: str, principal: str) -> AttemptRecord:
        record = self._store.get_attempt(run_id)
        if record is None or record.principal != principal:
            raise NotFound("run not found")
        return record

    def events(
        self, run_id: str, principal: str, *, after: int = 0, limit: int = 1000
    ) -> tuple[AttemptRecord, list[EventRecord]]:
        record = self.owned_attempt(run_id, principal)
        capped = min(limit, self._config.api.limits.max_events_returned)
        return record, self._store.list_events(run_id, after=after, limit=capped)

    def owned_artifact(self, artifact_id: str, principal: str) -> ArtifactRecord:
        artifact = self._store.get_artifact(artifact_id)
        if artifact is None or artifact.principal != principal:
            raise NotFound("artifact not found")
        return artifact

    def active_run(self, run_id: str) -> ActiveRun | None:
        return self._active.get(run_id)


def _verification(result: Any) -> dict[str, Any]:
    verification = result.verification
    return {
        "status": verification.status.value,
        "source": verification.source,
        "reason": verification.reason,
    }


def _usage(result: Any) -> dict[str, Any]:
    usage = result.usage
    return {
        "provenance": usage.provenance.value,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }
