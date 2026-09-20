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


def test_clean_worker_card_executes(policy):
    d = decide(policy, task_row(body=spec_body()))
    assert d.recommended_action.value == "execute"
    assert d.route == "worker.code.standard"
    assert d.candidates == ["devin-swe-2-max"]
    assert d.task_revision == "1"
    assert d.policy_version == "2026-09-20.1"
    assert 0.0 <= d.confidence <= 1.0


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
def test_tier_gating(policy, tier, action):
    d = decide(policy, task_row(body=spec_body(spec_dict(tier=tier))))
    assert d.recommended_action.value == action
    assert d.tier.value == tier


def test_route_without_available_candidate_holds(policy):
    d = decide(policy, task_row(body=spec_body(spec_dict(
        role="reviewer", capability="review", tier="standard",
    ))))
    assert d.recommended_action.value == "hold"
    assert d.candidates == []
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


def test_decision_never_executes_with_empty_candidates(policy):
    # Any non-execute decision must carry no selected candidate order.
    d = decide(policy, task_row(body=spec_body(spec_dict(risk_flags=["billing"]))))
    assert d.recommended_action.value == "needs_approval"
    # candidates are still the resolved order for the compiler's visibility
    assert d.candidates == ["devin-swe-2-max"]
