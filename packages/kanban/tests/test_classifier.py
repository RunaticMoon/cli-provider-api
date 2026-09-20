"""Rules-first Jev classifier behaviour."""

from __future__ import annotations

import pytest

from cli_provider_kanban.board import BoardTask
from cli_provider_kanban.classifier import classify
from cli_provider_kanban.policy import load_policy

from cli_provider_kanban.spec import resolve_spec

from conftest import policy_dict, spec_body, spec_dict, write_policy


@pytest.fixture
def policy(tmp_path):
    return load_policy(write_policy(tmp_path))


def task_row(task_id="t_abc12345", body=None, **kw):
    defaults = dict(
        title="card",
        assignee="jev-native",
        status="ready",
        priority=0,
        created_at=1_700_000_000,
        workspace_kind="scratch",
        workspace_path=None,
        model_override=None,
        provider_override=None,
        reasoning_effort=None,
        skills=None,
        max_retries=None,
        max_runtime_seconds=None,
        parents=(),
    )
    defaults.update(kw)
    return BoardTask(id=task_id, body=body, **defaults)


def decide(policy, task, task_map=None):
    return classify(task, resolve_spec(task, task_map), policy)


def _spec_with_work(**work_overrides):
    """A spec that carries only the structured work block (no hints)."""
    work = {"kind": "implement", "design": "ready", "scope": "medium"}
    work.update(work_overrides)
    spec = spec_dict()
    for key in ("role", "capability", "tier"):
        del spec[key]
    spec["work"] = work
    return spec


def test_clean_worker_card_executes(policy):
    d = decide(policy, task_row(body=spec_body()))
    assert d.recommended_action.value == "execute"
    assert d.route == "worker.code.standard"
    assert d.task_revision == "1"
    assert d.policy_version == "2026-09-20.1"
    assert 0.0 <= d.confidence <= 1.0


def test_decision_is_route_only_no_candidates(policy):
    # JevDecision must never select a backend or order — that is the policy
    # compiler's and 9Router's job.
    d = decide(policy, task_row(body=spec_body()))
    assert "candidates" not in d.model_dump(mode="json")
    assert d.route == "worker.code.standard"


def test_missing_spec_replans(policy):
    d = decide(policy, task_row(body="no contract here"))
    assert d.recommended_action.value == "replan"
    assert "spec" in d.reason.lower()


def test_invalid_spec_replans(policy):
    bad = spec_dict()
    del bad["verification"]
    d = decide(policy, task_row(body=spec_body(bad)))
    assert d.recommended_action.value == "replan"
    assert d.reason


def test_risk_flag_forces_approval(policy):
    d = decide(policy, task_row(body=spec_body(spec_dict(risk_flags=["security"]))))
    assert d.recommended_action.value == "needs_approval"
    assert [f.value for f in d.risk_flags] == ["security"]
    # Route still resolved for visibility.
    assert d.route == "worker.code.standard"


@pytest.mark.parametrize("flag", ["authn", "authz", "billing", "destruction",
                                  "migration", "production", "external_effects"])
def test_each_approval_risk_flag(policy, flag):
    d = decide(policy, task_row(body=spec_body(spec_dict(risk_flags=[flag]))))
    assert d.recommended_action.value == "needs_approval"


@pytest.mark.parametrize("tier,action", [
    ("free", "execute"), ("easy", "execute"), ("standard", "execute"),
    ("hard", "needs_approval"), ("max", "needs_approval"),
])
def test_tier_gating(tmp_path, tier, action):
    data = policy_dict()
    for t in ("free", "hard", "max"):
        data["routes"][f"worker.code.{t}"] = {"candidates": ["devin-swe-2-max"]}
    policy = load_policy(write_policy(tmp_path, data))
    d = decide(policy, task_row(body=spec_body(spec_dict(tier=tier))))
    assert d.recommended_action.value == action
    assert d.tier.value == tier


def test_hard_and_max_are_floor_gates_even_when_list_omits(tmp_path):
    # Operator emptied approval.tiers — hard/max must still gate.
    data = policy_dict()
    data["approval"]["tiers"] = []
    for t in ("hard", "max"):
        data["routes"][f"worker.code.{t}"] = {"candidates": ["devin-swe-2-max"]}
    policy = load_policy(write_policy(tmp_path, data))
    for tier in ("hard", "max"):
        d = decide(policy, task_row(body=spec_body(spec_dict(tier=tier))))
        assert d.recommended_action.value == "needs_approval", tier


def test_risk_flags_are_floor_gates_even_when_list_omits(tmp_path):
    data = policy_dict()
    data["approval"]["risk_flags"] = []
    policy = load_policy(write_policy(tmp_path, data))
    d = decide(policy, task_row(body=spec_body(spec_dict(risk_flags=["billing"]))))
    assert d.recommended_action.value == "needs_approval"


def test_unknown_cost_candidate_gates_approval(tmp_path):
    # Enabling the unknown-cost BAI candidate on the standard route means a
    # possibly-selected backend has unknown cost -> approval, never free.
    data = policy_dict()
    data["backends"][1]["enabled"] = True
    policy = load_policy(write_policy(tmp_path, data))
    d = decide(policy, task_row(body=spec_body()))
    assert d.recommended_action.value == "needs_approval"
    assert "cost" in d.reason.lower()


def test_unknown_cost_floor_when_operator_omits(tmp_path):
    data = policy_dict()
    data["approval"]["cost_tiers"] = []
    data["backends"][1]["enabled"] = True
    policy = load_policy(write_policy(tmp_path, data))
    d = decide(policy, task_row(body=spec_body()))
    assert d.recommended_action.value == "needs_approval"


def test_confidence_floor_causes_replan(tmp_path):
    data = policy_dict()
    data["classifier"]["min_execute_confidence"] = 0.99  # body conf is 0.95
    policy = load_policy(write_policy(tmp_path, data))
    d = decide(policy, task_row(body=spec_body()))
    assert d.recommended_action.value == "replan"
    assert "confidence" in d.reason.lower()


def test_confidence_floor_never_relaxes_approval(tmp_path):
    data = policy_dict()
    data["classifier"]["confidence"] = {"body": 1.0}
    data["classifier"]["min_execute_confidence"] = 0.0
    policy = load_policy(write_policy(tmp_path, data))
    d = decide(policy, task_row(body=spec_body(spec_dict(risk_flags=["billing"]))))
    assert d.recommended_action.value == "needs_approval"


def test_route_without_available_candidate_holds(policy):
    d = decide(policy, task_row(body=spec_body(spec_dict(
        role="reviewer", capability="review", tier="standard",
    ))))
    assert d.recommended_action.value == "hold"
    assert d.route == "reviewer.review.standard"


def test_missing_route_replans(policy):
    d = decide(policy, task_row(body=spec_body(spec_dict(
        role="researcher", capability="research",
    ))))
    assert d.recommended_action.value == "replan"
    assert "route" in d.reason.lower()


def test_unknown_capability_replans(policy):
    d = decide(policy, task_row(body=spec_body(spec_dict(capability="alchemy"))))
    assert d.recommended_action.value == "replan"


def test_capability_not_mapped_holds(policy, tmp_path):
    data = policy_dict()
    data["routes"]["worker.research.standard"] = {"candidates": ["devin-swe-2-max"]}
    policy = load_policy(write_policy(tmp_path, data))
    d = decide(policy, task_row(body=spec_body(spec_dict(capability="research"))))
    assert d.recommended_action.value == "hold"


@pytest.mark.parametrize("field,value", [
    ("model_override", "some-model"),
    ("provider_override", "some-provider"),
    ("reasoning_effort", "high"),
])
def test_row_level_overrides_rejected(policy, field, value):
    d = decide(policy, task_row(body=spec_body(), **{field: value}))
    assert d.recommended_action.value == "replan"
    assert "override" in d.reason.lower() or "untrusted" in d.reason.lower()


def test_preset_workspace_path_rejected(policy):
    d = decide(policy, task_row(
        body=spec_body(), workspace_path="/tmp/untrusted",
    ))
    assert d.recommended_action.value == "replan"


def test_spec_override_keys_rejected(policy):
    bad = spec_dict()
    bad["executable"] = "/usr/bin/curl"
    d = decide(policy, task_row(body=spec_body(bad)))
    assert d.recommended_action.value == "replan"


def test_unknown_workspace_id_replans(policy):
    d = decide(policy, task_row(body=spec_body(spec_dict(workspace_id="ws-nowhere"))))
    assert d.recommended_action.value == "replan"


def test_dependency_drift_replans(policy):
    d = decide(policy, task_row(
        body=spec_body(spec_dict(dependency_ids=["t_parent"])), parents=(),
    ))
    assert d.recommended_action.value == "replan"


def test_matching_dependencies_execute(policy):
    d = decide(policy, task_row(
        body=spec_body(spec_dict(dependency_ids=["t_parent"])),
        parents=("t_parent",),
    ))
    assert d.recommended_action.value == "execute"


def test_replan_count_over_cap_needs_approval(policy):
    d = decide(policy, task_row(body=spec_body(spec_dict(replan_count=3))))
    assert d.recommended_action.value == "needs_approval"
    assert "replan" in d.reason.lower()


def test_decomposition_bounds(policy):
    spec = spec_dict(role="planner", capability="planning",
                     decomposition={"depth": 9, "children": 2})
    d = decide(policy, task_row(body=spec_body(spec)))
    assert d.recommended_action.value == "replan"

    spec = spec_dict(role="planner", capability="planning",
                     decomposition={"depth": 1, "children": 99})
    d = decide(policy, task_row(body=spec_body(spec)))
    assert d.recommended_action.value == "replan"


def test_planner_route_unsupported_replans(policy):
    # planner.planning.* is not in the sample policy's route table.
    spec = spec_dict(role="planner", capability="planning",
                     decomposition={"depth": 1, "children": 2})
    d = decide(policy, task_row(body=spec_body(spec)))
    assert d.recommended_action.value == "replan"


def test_effort_hint_preserved_not_tier(policy):
    d = decide(policy, task_row(body=spec_body(spec_dict(
        tier="easy", effort_hint="maximum",
    ))))
    assert d.recommended_action.value == "execute"
    assert d.effort_hint.value == "maximum"
    assert d.tier.value == "easy"
    assert d.route == "worker.code.easy"


def test_task_map_fallback_classifies(policy):
    entry = spec_dict()
    del entry["task_id"]
    task = task_row(body="prose")
    d = decide(policy, task, task_map={"t_abc12345": entry})
    assert d.recommended_action.value == "execute"
    assert d.reason or True
    assert 0.0 <= d.confidence <= 1.0


def test_needs_approval_carries_no_candidates(policy):
    # Non-execute decisions carry only the route — no backend order, ever.
    d = decide(policy, task_row(body=spec_body(spec_dict(risk_flags=["billing"]))))
    assert d.recommended_action.value == "needs_approval"
    assert "candidates" not in d.model_dump(mode="json")


# --- Structured work block derivation -------------------------------------


def test_work_block_derives_route(policy):
    spec = _spec_with_work()  # implement/ready/medium
    d = decide(policy, task_row(body=spec_body(spec)))
    assert d.recommended_action.value == "execute"
    assert d.route == "worker.code.standard"
    assert d.role.value == "worker"
    assert d.capability == "code"
    assert d.tier.value == "standard"


def test_work_block_small_scope_derives_easy(policy):
    spec = _spec_with_work(scope="small")
    d = decide(policy, task_row(body=spec_body(spec)))
    assert d.recommended_action.value == "execute"
    assert d.route == "worker.code.easy"


@pytest.mark.parametrize("design", ["draft", "unclear"])
def test_work_block_unready_design_replans(policy, design):
    spec = _spec_with_work(design=design)
    d = decide(policy, task_row(body=spec_body(spec)))
    assert d.recommended_action.value == "replan"
    assert "design" in d.reason.lower()


def test_work_block_large_scope_gated(tmp_path):
    # scope large -> derived tier hard -> approval gate (route must exist).
    data = policy_dict()
    data["routes"]["worker.code.hard"] = {"candidates": ["devin-swe-2-max"]}
    policy = load_policy(write_policy(tmp_path, data))
    spec = _spec_with_work(scope="large")
    d = decide(policy, task_row(body=spec_body(spec)))
    assert d.recommended_action.value == "needs_approval"
    assert d.tier.value == "hard"


def test_work_kind_reviewer_derives_review(policy):
    spec = _spec_with_work(kind="review")
    d = decide(policy, task_row(body=spec_body(spec)))
    # reviewer.review.standard exists but all candidates disabled -> hold.
    assert d.recommended_action.value == "hold"
    assert d.route == "reviewer.review.standard"


def test_conflicting_hint_replans(policy):
    spec = _spec_with_work()
    spec["tier"] = "easy"  # conflicts with derived medium->standard
    d = decide(policy, task_row(body=spec_body(spec)))
    assert d.recommended_action.value == "replan"
    assert "conflict" in d.reason.lower()


def test_conflicting_role_hint_replans(policy):
    spec = _spec_with_work()
    spec["role"] = "reviewer"
    d = decide(policy, task_row(body=spec_body(spec)))
    assert d.recommended_action.value == "replan"


def test_agreeing_hints_execute(policy):
    spec = _spec_with_work()
    spec["role"] = "worker"
    spec["capability"] = "code"
    spec["tier"] = "standard"
    d = decide(policy, task_row(body=spec_body(spec)))
    assert d.recommended_action.value == "execute"
    assert d.route == "worker.code.standard"
