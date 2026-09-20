"""TaskSpec strict schema + extraction from card bodies / task map."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cli_provider_kanban.models import TaskSpec
from cli_provider_kanban.spec import resolve_spec

from conftest import spec_body, spec_dict


def _task(task_id="t_abc12345", body=None):
    """Minimal stand-in for a board row as seen by the classifier."""

    class _Row:
        pass

    row = _Row()
    row.id = task_id
    row.body = body
    return row


def test_valid_spec_parses():
    spec = TaskSpec.model_validate(spec_dict())
    assert spec.task_id == "t_abc12345"
    assert spec.tier.value == "standard"
    assert spec.effort_hint.value == "balanced"


@pytest.mark.parametrize(
    "missing",
    [
        "objective", "inputs", "dependency_ids", "relevant_files",
        "allowed_scope", "artifacts", "verification", "acceptance_criteria",
        "prohibited", "base_revision", "workspace_id", "risk_flags",
        "task_revision", "role", "capability", "tier",
    ],
)
def test_required_contract_fields(missing):
    data = spec_dict()
    del data[missing]
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(data)


def test_verification_argv_must_be_list_not_shell_string():
    data = spec_dict()
    data["verification"] = {"argv": "uv run pytest", "criteria": "exit 0"}
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(data)


def test_verification_requires_criteria():
    data = spec_dict()
    data["verification"] = {"argv": ["true"], "criteria": ""}
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(data)


@pytest.mark.parametrize("bad_key", ["executable", "model", "provider", "workspace_path", "command", "env"])
def test_untrusted_override_keys_rejected(bad_key):
    data = spec_dict()
    data[bad_key] = "/tmp/evil" if bad_key != "env" else {"A": "B"}
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(data)


def test_tier_is_one_of_five():
    for tier in ("free", "easy", "standard", "hard", "max"):
        assert TaskSpec.model_validate(spec_dict(tier=tier)).tier.value == tier
    for bad in ("ultra", "maximum", "high", "none", 3):
        with pytest.raises(ValidationError):
            TaskSpec.model_validate(spec_dict(tier=bad))


def test_effort_hint_separate_from_tier():
    spec = TaskSpec.model_validate(spec_dict(effort_hint="maximum", tier="easy"))
    assert spec.effort_hint.value == "maximum"
    assert spec.tier.value == "easy"
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(spec_dict(effort_hint="standard"))
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(spec_dict(effort_hint="hard"))


def test_risk_flags_controlled_vocabulary():
    spec = TaskSpec.model_validate(spec_dict(risk_flags=["security", "production"]))
    assert [f.value for f in spec.risk_flags] == ["security", "production"]
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(spec_dict(risk_flags=["scary"]))


def test_task_revision_accepts_str_or_int():
    assert TaskSpec.model_validate(spec_dict(task_revision=7)).task_revision == "7"
    assert TaskSpec.model_validate(spec_dict(task_revision="rev-2")).task_revision == "rev-2"


def test_resolve_spec_from_fenced_block():
    result = resolve_spec(_task(body=spec_body()), task_map=None)
    assert result.spec is not None
    assert result.source == "body"
    assert result.errors == ()


def test_resolve_spec_from_whole_body_json():
    result = resolve_spec(_task(body=json.dumps(spec_dict())), task_map=None)
    assert result.spec is not None
    assert result.source == "body"


def test_resolve_spec_task_id_mismatch():
    result = resolve_spec(_task(task_id="t_other", body=spec_body()), task_map=None)
    assert result.spec is None
    assert result.errors


def test_resolve_spec_no_contract_anywhere():
    result = resolve_spec(_task(body="just prose"), task_map=None)
    assert result.spec is None
    assert result.source == "none"
    assert result.errors == ()


def test_resolve_spec_invalid_json_block():
    result = resolve_spec(
        _task(body="```jev-task-spec\n{not json\n```\n"), task_map=None
    )
    assert result.spec is None
    assert result.errors


def test_resolve_spec_rejects_multiple_blocks():
    body = spec_body() + "\n" + spec_body(spec_dict(task_revision="2"))
    result = resolve_spec(_task(body=body), task_map=None)
    assert result.spec is None
    assert result.errors


def test_resolve_spec_from_task_map():
    entry = spec_dict(task_id="t_abc12345")
    del entry["task_id"]  # implied by the mapping key
    result = resolve_spec(_task(body="prose only"), task_map={"t_abc12345": entry})
    assert result.spec is not None
    assert result.source == "task_map"
    assert result.spec.task_id == "t_abc12345"


def test_task_map_entry_with_conflicting_id_rejected():
    result = resolve_spec(
        _task(body="prose"), task_map={"t_abc12345": spec_dict(task_id="t_other")}
    )
    assert result.spec is None
    assert result.errors


def test_body_over_byte_limit_rejected():
    big = "x" * 70000
    result = resolve_spec(_task(body=big), task_map=None, max_body_bytes=65536)
    assert result.spec is None
    assert result.errors
