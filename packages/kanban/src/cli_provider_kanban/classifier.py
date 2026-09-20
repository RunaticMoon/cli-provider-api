"""Rules-first Jev classifier.

Deterministic rules only — there is no LLM path and no candidate fallback
logic. Anything ambiguous, invalid, overridden or unroutable is ``replan``;
risk/tier gates yield ``needs_approval``; a known route with no available
candidate yields ``hold``. The classifier never touches the card graph.
"""

from __future__ import annotations

from .board import BoardTask
from .models import (
    EffortHint,
    JevDecision,
    RecommendedAction,
    Role,
    Tier,
)
from .policy import Policy, available_candidates, route_key
from .spec import SpecResult

# Row columns a card must never set: the operator/policy owns execution
# selection, so a card pinning its own model/provider/effort/workspace is
# rejected as an untrusted override.
_ROW_OVERRIDE_FIELDS = (
    "model_override",
    "provider_override",
    "reasoning_effort",
)

def _confidence(source: str, policy: Policy) -> float:
    conf = policy.classifier.confidence
    return {
        "body": conf.body,
        "task_map": conf.task_map,
    }.get(source, conf.no_spec)


def classify(
    task: BoardTask,
    spec_result: SpecResult,
    policy: Policy,
) -> JevDecision:
    """One card -> one logical decision. Never raises on card content."""
    spec = spec_result.spec
    revision = spec.task_revision if spec is not None else "0"
    role = spec.role if spec is not None else Role.WORKER
    base = dict(
        task_id=task.id,
        task_revision=revision,
        role=role,
        confidence=_confidence(spec_result.source, policy),
        policy_version=policy.policy_version,
        risk_flags=list(spec.risk_flags) if spec is not None else [],
        candidates=[],
    )

    def replan(reason: str) -> JevDecision:
        return JevDecision(
            **base,
            capability=spec.capability if spec else None,
            tier=spec.tier if spec else None,
            effort_hint=spec.effort_hint if spec else None,
            route=None,
            recommended_action=RecommendedAction.REPLAN,
            reason=reason,
        )

    # 1. Card-level untrusted overrides (model/provider/effort/workspace path).
    violations = [
        f"tasks.{field} is set"
        for field in _ROW_OVERRIDE_FIELDS
        if getattr(task, field, None)
    ]
    if task.workspace_path:
        violations.append("tasks.workspace_path is pre-set")
    if violations:
        return replan(
            "untrusted card overrides rejected: " + "; ".join(violations)
            + " — executor/workspace/model are operator-owned"
        )

    # 2. Spec resolution/validation problems -> replan.
    if spec_result.errors:
        return replan("invalid task spec: " + "; ".join(spec_result.errors))
    if spec is None:
        return replan(
            "card carries no structured jev-task-spec and no task_map entry"
        )

    if spec.task_id != task.id:
        return replan(
            f"spec task_id {spec.task_id!r} does not match card {task.id!r}"
        )
    if spec.capability not in set(policy.capabilities):
        return replan(
            f"capability {spec.capability!r} is not in policy.capabilities"
        )
    if spec.workspace_id not in policy.workspaces:
        return replan(
            f"workspace_id {spec.workspace_id!r} is not a trusted policy workspace"
        )
    if set(spec.dependency_ids) != set(task.parents):
        return replan(
            "dependency_ids do not match the card's board parents "
            f"(declared={sorted(spec.dependency_ids)}, "
            f"board={sorted(task.parents)})"
        )
    if spec.decomposition is not None:
        if spec.decomposition.depth > policy.decomposition.max_depth:
            return replan(
                f"decomposition depth {spec.decomposition.depth} exceeds "
                f"policy max_depth {policy.decomposition.max_depth}"
            )
        if spec.decomposition.children > policy.decomposition.max_children:
            return replan(
                f"decomposition children {spec.decomposition.children} exceeds "
                f"policy max_children {policy.decomposition.max_children}"
            )

    route = route_key(spec.role, spec.capability, spec.tier)
    route_cfg = policy.routes.get(route)
    if route_cfg is None:
        return replan(f"no route {route!r} exists in the routing policy")
    available = available_candidates(policy, route_cfg, spec.capability)
    resolved = dict(
        base,
        capability=spec.capability,
        tier=spec.tier,
        effort_hint=spec.effort_hint,
        route=route,
        candidates=[b.id for b in available],
    )

    # 3. Approval gates: replan-cap overrun, declared risk flags, gated tiers.
    #    These cannot be overridden by confidence.
    if spec.replan_count > policy.decomposition.replan_cap:
        return JevDecision(
            **resolved,
            recommended_action=RecommendedAction.NEEDS_APPROVAL,
            reason=(
                f"replan_count {spec.replan_count} exceeds policy replan_cap "
                f"{policy.decomposition.replan_cap} — human review required"
            ),
        )
    gated_risks = sorted(
        {f.value for f in spec.risk_flags}
        & {f.value for f in policy.approval.risk_flags}
    )
    if gated_risks:
        return JevDecision(
            **resolved,
            recommended_action=RecommendedAction.NEEDS_APPROVAL,
            reason=f"risk flags {gated_risks} require user approval",
        )
    if spec.tier in set(policy.approval.tiers):
        return JevDecision(
            **resolved,
            recommended_action=RecommendedAction.NEEDS_APPROVAL,
            reason=f"tier {spec.tier.value!r} requires user approval",
        )

    # 4. Route exists but nothing is currently enabled+mapped -> hold.
    if not available:
        disabled = [
            bid
            for bid in route_cfg.candidates
            if not policy.backend_map()[bid].enabled
        ]
        return JevDecision(
            **resolved,
            recommended_action=RecommendedAction.HOLD,
            reason=(
                f"route {route!r} has no available candidate "
                f"(disabled/unmapped: {disabled or route_cfg.candidates})"
            ),
        )

    return JevDecision(
        **resolved,
        recommended_action=RecommendedAction.EXECUTE,
        reason=(
            f"contract complete; route {route!r} resolved with "
            f"{len(available)} available candidate(s)"
        ),
    )


__all__ = ["classify", "EffortHint", "Tier"]
