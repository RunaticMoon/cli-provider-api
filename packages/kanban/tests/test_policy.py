"""Policy schema + semantic validation (central routing policy)."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from cli_provider_kanban.policy import (
    Policy,
    available_candidates,
    load_policy,
    route_key,
)

from conftest import policy_dict, write_policy


def test_valid_policy_loads(tmp_path):
    policy = load_policy(write_policy(tmp_path))
    assert policy.policy_version == "2026-09-20.1"
    assert policy.scope.assignee == "jev-native"
    assert policy.backends[0].id == "devin-swe-2-max"


def test_codex_backend_kind_rejected(tmp_path):
    data = policy_dict()
    data["backends"] = [
        {"id": "codex-cli", "kind": "codex", "enabled": True, "capabilities": {"code": True}}
    ]
    data["routes"] = {"worker.code.standard": {"candidates": ["codex-cli"]}}
    with pytest.raises(ValidationError):
        load_policy(write_policy(tmp_path, data))


def test_codex_model_or_id_rejected(tmp_path):
    data = policy_dict()
    data["backends"][0]["model"] = "codex-5"
    with pytest.raises(ValidationError, match="[Cc]odex"):
        load_policy(write_policy(tmp_path, data))


@pytest.mark.parametrize(
    "key",
    ["worker.code.ultra", "worker.code", "boss.code.standard",
     "worker.UNKNOWNCAP.standard", "worker.code.standard.extra"],
)
def test_route_key_validation(tmp_path, key):
    data = policy_dict()
    data["routes"] = {key: {"candidates": ["devin-swe-2-max"]}}
    with pytest.raises(ValidationError):
        load_policy(write_policy(tmp_path, data))


def test_unknown_candidate_backend_rejected(tmp_path):
    data = policy_dict()
    data["routes"]["worker.code.standard"] = {"candidates": ["ghost-backend"]}
    with pytest.raises(ValidationError):
        load_policy(write_policy(tmp_path, data))


def test_llm_classifier_other_than_disabled_rejected(tmp_path):
    data = policy_dict()
    data["classifier"] = {"llm": "enabled"}
    with pytest.raises(ValidationError):
        load_policy(write_policy(tmp_path, data))


def test_assignee_must_not_be_default_profile(tmp_path):
    data = policy_dict()
    data["scope"] = {"assignee": "default", "statuses": ["ready"]}
    with pytest.raises(ValidationError, match="profile"):
        load_policy(write_policy(tmp_path, data))


def test_assignee_must_not_collide_with_existing_profile(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes_home"
    (hermes_home / "profiles" / "jev-native").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    with pytest.raises(ValidationError, match="profile"):
        load_policy(write_policy(tmp_path))


def test_assignee_ok_when_profile_dir_absent(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    policy = load_policy(write_policy(tmp_path))
    assert policy.scope.assignee == "jev-native"


def test_duplicate_backend_id_rejected(tmp_path):
    data = policy_dict()
    data["backends"].append(dict(data["backends"][0]))
    with pytest.raises(ValidationError):
        load_policy(write_policy(tmp_path, data))


@pytest.mark.parametrize("key", ["repo", "worktree_root"])
def test_workspace_roots_must_be_absolute(tmp_path, key):
    data = policy_dict()
    data["workspaces"] = {
        "ws-main": {
            "repo": "/abs/repo",
            "worktree_root": "/abs/wt",
            "wrapper_workspace_id": "ws-alpha",
            key: "relative/dir",
        }
    }
    with pytest.raises(ValidationError):
        load_policy(write_policy(tmp_path, data))


def test_scope_status_must_be_real_kanban_status(tmp_path):
    data = policy_dict()
    data["scope"] = {"assignee": "jev-native", "statuses": ["flying"]}
    with pytest.raises(ValidationError):
        load_policy(write_policy(tmp_path, data))


def test_candidate_order_preserved_and_disabled_filtered(tmp_path):
    policy = load_policy(write_policy(tmp_path))
    # worker.code.standard lists [devin-swe-2-max, bai-flash]; bai is disabled.
    route = policy.routes[route_key("worker", "code", "standard")]
    assert route.candidates == ["devin-swe-2-max", "bai-flash"]
    available = available_candidates(policy, route, "code")
    assert [b.id for b in available] == ["devin-swe-2-max"]


def test_capability_mapping_false_filters_candidate(tmp_path):
    data = policy_dict()
    data["routes"]["worker.research.standard"] = {"candidates": ["devin-swe-2-max"]}
    policy = load_policy(write_policy(tmp_path, data))
    route = policy.routes["worker.research.standard"]
    # swe-2-max mapping: research=False -> no available candidate -> hold.
    assert available_candidates(policy, route, "research") == []


def test_reviewer_route_all_disabled_is_hold_ready(tmp_path):
    policy = load_policy(write_policy(tmp_path))
    route = policy.routes["reviewer.review.standard"]
    assert route.candidates == ["devin-opus-review"]
    assert available_candidates(policy, route, "review") == []


def test_missing_policy_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_policy(tmp_path / "nope.yaml")


def test_policy_rejects_unexpected_root_key(tmp_path):
    data = policy_dict()
    data["surprise"] = True
    with pytest.raises(ValidationError):
        load_policy(write_policy(tmp_path, data))


def test_json_policy_also_loads(tmp_path):
    import json

    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy_dict()), encoding="utf-8")
    assert load_policy(path).policy_version == "2026-09-20.1"


def test_missing_policy_version_rejected(tmp_path):
    data = policy_dict()
    del data["policy_version"]
    with pytest.raises(ValidationError):
        load_policy(write_policy(tmp_path, data))


def test_yaml_load(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(policy_dict()), encoding="utf-8")
    assert isinstance(load_policy(path), Policy)


# --- Backend transport / effort maps ----------------------------------------


def test_api_backends_declared_with_api_transport(tmp_path):
    policy = load_policy(write_policy(tmp_path))
    by_id = {b.id: b for b in policy.backends}
    assert by_id["bai-flash"].kind == "bai"
    assert by_id["bai-flash"].transport == "api"
    assert by_id["commandcode-flash"].transport == "api"
    assert by_id["devin-swe-2-max"].transport == "native"
    # Model ids are the verified ops values, not invented.
    assert by_id["bai-flash"].model == "deepseek-v4.1-flash"
    assert by_id["commandcode-flash"].model == "deepseek/deepseek-v4.1-flash"


def test_transport_must_match_kind(tmp_path):
    data = policy_dict()
    data["backends"][0]["transport"] = "api"  # devin is native-only
    with pytest.raises(ValidationError, match="transport"):
        load_policy(write_policy(tmp_path, data))

    data = policy_dict()
    data["backends"][1]["transport"] = "native"  # bai is api-only
    with pytest.raises(ValidationError, match="transport"):
        load_policy(write_policy(tmp_path, data))


def test_api_backend_requires_preset(tmp_path):
    data = policy_dict()
    data["backends"][1]["preset"] = None
    with pytest.raises(ValidationError, match="preset"):
        load_policy(write_policy(tmp_path, data))


def test_native_backend_rejects_api_only_effort(tmp_path):
    data = policy_dict()
    data["backends"][0]["effort_map"] = {"auto": "high"}  # devin wire values
    with pytest.raises(ValidationError, match="effort"):
        load_policy(write_policy(tmp_path, data))


def test_api_backend_rejects_native_effort_wire(tmp_path):
    data = policy_dict()
    data["backends"][1]["effort_map"] = {"auto": "swe-2-fast"}
    with pytest.raises(ValidationError, match="effort"):
        load_policy(write_policy(tmp_path, data))


def test_effort_map_rejects_unknown_hint(tmp_path):
    data = policy_dict()
    data["backends"][0]["effort_map"] = {"turbo": "swe-2-fast"}
    with pytest.raises(ValidationError, match="effort"):
        load_policy(write_policy(tmp_path, data))


def test_resolve_effort_devin_auto_maps_null(tmp_path):
    from cli_provider_kanban.models import EffortHint
    from cli_provider_kanban.policy import resolve_effort

    policy = load_policy(write_policy(tmp_path))
    route = policy.routes["worker.code.standard"]
    # bai-flash is disabled -> skipped; devin auto maps to no-flag null.
    assert resolve_effort(policy, route, "code", EffortHint.AUTO) == {
        "devin-swe-2-max": None
    }


def test_resolve_effort_api_maps_declared_values(tmp_path):
    from cli_provider_kanban.models import EffortHint
    from cli_provider_kanban.policy import resolve_effort

    data = policy_dict()
    data["backends"][1]["enabled"] = True
    data["routes"]["worker.code.max"] = {"candidates": ["bai-flash"]}
    policy = load_policy(write_policy(tmp_path, data))
    resolved = resolve_effort(
        policy, policy.routes["worker.code.standard"], "code", EffortHint.AUTO
    )
    assert resolved == {"devin-swe-2-max": None, "bai-flash": "low"}
    resolved = resolve_effort(
        policy, policy.routes["worker.code.max"], "code", EffortHint.MAXIMUM
    )
    assert resolved == {"bai-flash": "max"}


def test_resolve_effort_unsupported_hint_raises(tmp_path):
    from cli_provider_kanban.models import EffortHint
    from cli_provider_kanban.policy import EffortUnsupported, resolve_effort

    policy = load_policy(write_policy(tmp_path))
    route = policy.routes["worker.code.standard"]
    # devin maps 'auto' implicitly; 'economy' has no verified mapping.
    with pytest.raises(EffortUnsupported):
        resolve_effort(policy, route, "code", EffortHint.ECONOMY)


def test_api_backend_with_empty_effort_map_rejects_any_hint(tmp_path):
    from cli_provider_kanban.models import EffortHint
    from cli_provider_kanban.policy import EffortUnsupported, resolve_effort

    data = policy_dict()
    data["backends"][1]["enabled"] = True
    data["backends"][1]["effort_map"] = {}
    policy = load_policy(write_policy(tmp_path, data))
    # easy route orders bai-flash first; an api backend with no mapping must
    # fail before claim rather than guess a wire value.
    with pytest.raises(EffortUnsupported):
        resolve_effort(
            policy, policy.routes["worker.code.easy"], "code", EffortHint.AUTO
        )


def test_backend_kind_rejects_unknown(tmp_path):
    data = policy_dict()
    data["backends"][0]["kind"] = "openrouter"
    with pytest.raises(ValidationError):
        load_policy(write_policy(tmp_path, data))


def test_workspace_wrapper_id_pattern(tmp_path):
    data = policy_dict()
    data["workspaces"]["ws-main"]["wrapper_workspace_id"] = "bad id!"
    with pytest.raises(ValidationError):
        load_policy(write_policy(tmp_path, data))


def test_policy_fingerprint_stable(tmp_path):
    from cli_provider_kanban.policy import policy_fingerprint

    policy = load_policy(write_policy(tmp_path))
    fp = policy_fingerprint(policy)
    assert fp.startswith("sha256:")
    assert fp == policy_fingerprint(policy)
    changed = policy_dict()
    changed["policy_version"] = "2026-09-21.1"
    other = load_policy(write_policy(tmp_path, changed, name="other.yaml"))
    assert policy_fingerprint(other) != fp
