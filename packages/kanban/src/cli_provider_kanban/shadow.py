"""Shadow read: classify in-scope cards without touching the board.

Reads the Hermes kanban SQLite through a ``mode=ro`` connection, classifies
each card under the scope assignee, and writes a deterministic JSON report.
Decisions are cached by ``(task_id, task_revision, policy_version)``; a card
mutated under an unchanged revision is reported as a ``conflict`` record and
never served from cache. The board is never written: no status changes, no
events, no schema migrations, no lock files.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import yaml

from .board import card_fingerprint, list_scope_tasks, open_readonly_board
from .cache import DecisionCache
from .classifier import classify
from .errors import ShadowError
from .models import JevDecision, RecommendedAction
from .policy import Policy, load_policy
from .spec import resolve_spec

__all__ = ["run_shadow", "ShadowError"]


def _load_task_map(policy: Policy, policy_path: Path) -> dict | None:
    if not policy.task_map:
        return None
    path = Path(policy.task_map)
    if not path.is_absolute():
        path = policy_path.parent / path
    if not path.is_file():
        raise ShadowError(f"policy task_map not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ShadowError(f"task_map {path} must be a mapping of task_id -> spec")
    return data


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _conflict_decision(task, spec, policy: Policy) -> JevDecision:
    return JevDecision(
        task_id=task.id,
        task_revision=spec.task_revision,
        role=spec.role,
        capability=spec.capability,
        tier=spec.tier,
        effort_hint=spec.effort_hint,
        route=None,
        recommended_action=RecommendedAction.REPLAN,
        risk_flags=list(spec.risk_flags),
        confidence=0.9,
        reason=(
            "card content changed but task_revision "
            f"{spec.task_revision!r} was not bumped — Lead must re-issue the "
            "spec with a new revision"
        ),
        policy_version=policy.policy_version,
        candidates=[],
    )


def run_shadow(
    *,
    board_db: str | Path,
    policy_path: str | Path,
    out_path: str | Path,
    cache_path: str | Path | None = None,
) -> dict:
    """Shadow-classify all in-scope cards; returns the report dict."""
    policy_file = Path(policy_path)
    policy = load_policy(policy_file)
    task_map = _load_task_map(policy, policy_file)

    board_path = Path(board_db)
    conn = open_readonly_board(board_path)
    try:
        tasks = list_scope_tasks(
            conn,
            assignee=policy.scope.assignee,
            statuses=policy.scope.statuses,
            limit=policy.limits.max_cards,
        )
    finally:
        conn.close()

    out = Path(out_path)
    cache = DecisionCache(
        Path(cache_path) if cache_path is not None
        else out.with_name(out.name + ".cache.json")
    )

    records = []
    for task in tasks:
        fingerprint = card_fingerprint(task)
        spec_result = resolve_spec(
            task,
            task_map,
            max_body_bytes=policy.limits.max_body_bytes,
            max_spec_bytes=policy.limits.max_spec_bytes,
        )
        decision: JevDecision | None = None
        provenance = "rules"
        if spec_result.spec is not None:
            entry = cache.lookup(
                task.id, spec_result.spec.task_revision, policy.policy_version
            )
            if entry is not None:
                if entry.fingerprint == fingerprint:
                    decision = JevDecision.model_validate(entry.decision)
                    provenance = "cache"
                else:
                    decision = _conflict_decision(task, spec_result.spec, policy)
                    provenance = "conflict"
        if decision is None:
            decision = classify(task, spec_result, policy)
            if spec_result.spec is not None:
                cache.store(
                    task.id,
                    spec_result.spec.task_revision,
                    policy.policy_version,
                    fingerprint,
                    decision.model_dump(mode="json"),
                )
        records.append(
            {
                "task_id": task.id,
                "status": task.status,
                "fingerprint": fingerprint,
                "provenance": provenance,
                "decision": decision.model_dump(mode="json"),
            }
        )

    report = {
        "schema_version": 1,
        "tool": "cli-provider-kanban",
        "mode": "shadow",
        "board_db": str(board_path),
        "assignee": policy.scope.assignee,
        "statuses": list(policy.scope.statuses),
        "policy_version": policy.policy_version,
        "records": records,
    }
    _write_json(out, report)
    cache.save()
    return report
