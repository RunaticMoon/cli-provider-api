"""Rules-first Jev classifier.

Deterministic rules only — there is no LLM path and no candidate selection
logic. Anything ambiguous, invalid, overridden or unroutable is ``replan``;
risk/tier/cost gates yield ``needs_approval``; a known route with no
available candidate yields ``hold``. The classifier never touches the card
graph and never emits a backend order — the decision is the logical route
only; candidate ordering belongs to the policy compiler and 9Router.
"""

from __future__ import annotations

from .board import BoardTask
from .models import (
    DesignReadiness,
    EffortHint,
    JevDecision,
    RecommendedAction,
    Role,
    ScopeSize,
    Tier,
    WorkKind,
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

# Structured work_kind -> (role, capability). Deterministic and total: any
# kind outside the enum is rejected by the schema before we ever see it.
_WORK_ROLE_CAPABILITY = {
    WorkKind.IMPLEMENT: (Role.WORKER, "code"),
    WorkKind.REVIEW: (Role.REVIEWER, "review"),
    WorkKind.RESEARCH: (Role.RESEARCHER, "research"),
    WorkKind.PLAN: (Role.PLANNER, "planning"),
}

# Declared scope -> routing tier. `free` and `max` are never derived: free is
# not a scope judgement and max is reserved for Lead-declared hints only.
_SCOPE_TIER = {
    ScopeSize.SMALL: Tier.EASY,
    ScopeSize.MEDIUM: Tier.STANDARD,
    ScopeSize.LARGE: Tier.HARD,
}


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
    role = spec.role if spec is not None and spec.role is not None else Role.WORKER
    base = dict(
        task_id=task.id,
        task_revision=revision,
        role=role,
        confidence=_confidence(spec_result.source, policy),
        policy_version=policy.policy_version,
        risk_flags=list(spec.risk_flags) if spec is not None else [],
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

    # 3. Route derivation. A structured work block derives role/capability/tier
    #    deterministically; the optional card hints are Lead intent and must
    #    agree with the derivation — a conflict is ambiguous -> replan. Without
    #    a work block the hints are the declared answer (schema guarantees all
    #    three are present together).
    if spec.work is not None:
        if spec.work.design is not DesignReadiness.READY:
            return replan(
                f"design readiness is {spec.work.design.value!r}, not 'ready' "
                "— the card needs planner/Lead rework before dispatch"
            )
        role, capability = _WORK_ROLE_CAPABILITY[spec.work.kind]
        tier = _SCOPE_TIER[spec.work.scope]
        conflicts = []
        if spec.role is not None and spec.role is not role:
            conflicts.append(
                f"role hint {spec.role.value!r} vs derived {role.value!r}"
            )
        if spec.capability is not None and spec.capability != capability:
            conflicts.append(
                f"capability hint {spec.capability!r} vs derived {capability!r}"
            )
        if spec.tier is not None and spec.tier is not tier:
            conflicts.append(
                f"tier hint {spec.tier.value!r} vs derived {tier.value!r}"
            )
        if conflicts:
            return replan(
                "ambiguous card: Lead intent hints conflict with the "
                "structured work block (" + "; ".join(conflicts) + ")"
            )
        base["role"] = role
    else:
        assert spec.role is not None and spec.capability is not None
        assert spec.tier is not None
        role, capability, tier = spec.role, spec.capability, spec.tier

    if capability not in set(policy.capabilities):
        return replan(
            f"capability {capability!r} is not in policy.capabilities"
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

    route = route_key(role, capability, tier)
    route_cfg = policy.routes.get(route)
    if route_cfg is None:
        return replan(f"no route {route!r} exists in the routing policy")
    available = available_candidates(policy, route_cfg, capability)
    resolved = dict(
        base,
        role=role,
        capability=capability,
        tier=tier,
        effort_hint=spec.effort_hint,
        route=route,
    )

    # 4. Approval gates: replan-cap overrun, any declared risk flag, gated
    #    tiers (hard/max are floor gates), gated cost across the available
    #    candidates. These cannot be overridden by confidence and cannot be
    #    relaxed by the operator lists.
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
        & {f.value for f in policy.approval.gated_risk_flags()}
    )
    if gated_risks:
        return JevDecision(
            **resolved,
            recommended_action=RecommendedAction.NEEDS_APPROVAL,
            reason=f"risk flags {gated_risks} require user approval",
        )
    if tier in policy.approval.gated_tiers():
        return JevDecision(
            **resolved,
            recommended_action=RecommendedAction.NEEDS_APPROVAL,
            reason=f"tier {tier.value!r} requires user approval",
        )

    # 5. Route exists but nothing is currently enabled+mapped -> hold.
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

    # 6. Cost gate: a candidate that could be selected with a gated (e.g.
    #    unknown) cost tier means the card may spend unapproved money.
    gated_costs = policy.approval.gated_cost_tiers()
    gated = [b.id for b in available if b.cost_tier in gated_costs]
    if gated:
        return JevDecision(
            **resolved,
            recommended_action=RecommendedAction.NEEDS_APPROVAL,
            reason=(
                f"routed candidate(s) {gated} carry a gated cost tier "
                f"{sorted(gated_costs)} — unknown/high cost requires approval, "
                "never treated as free"
            ),
        )

    if base["confidence"] < policy.classifier.min_execute_confidence:
        return replan(
            f"classifier confidence {base['confidence']:.2f} is below the "
            f"configured execute floor "
            f"{policy.classifier.min_execute_confidence:.2f} — replanning "
            "rather than guessing"
        )

    return JevDecision(
        **resolved,
        recommended_action=RecommendedAction.EXECUTE,
        reason=f"contract complete; route {route!r} resolved",
    )


__all__ = ["classify", "EffortHint", "Tier"]
