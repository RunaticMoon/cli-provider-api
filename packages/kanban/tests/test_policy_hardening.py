"""Policy hardening — new fail-closed fields for workspace binding, control
identity, verification argv allowlist and the execution control seam."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from cli_provider_kanban.policy import Policy

from conftest import CURRENT_OS_USER, policy_dict


def _load(data: dict) -> Policy:
    return Policy.model_validate(data)


class TestExecutionControlSeam:
    def test_direct_defaults_control_to_base(self):
        data = policy_dict()
        data["execution"] = {"mode": "direct",
                             "base_url": "http://127.0.0.1:9",
                             "model": "devin/swe-2-max"}
        p = _load(data)
        assert p.execution.control_base_url is None  # falls back to base

    def test_gateway_requires_control_base_url(self):
        data = policy_dict()
        data["execution"] = {"mode": "gateway",
                             "base_url": "http://127.0.0.1:9"}
        with pytest.raises(ValidationError):
            _load(data)

    def test_gateway_rejects_static_model(self):
        data = policy_dict()
        data["execution"] = {
            "mode": "gateway",
            "base_url": "http://127.0.0.1:9",
            "control_base_url": "http://127.0.0.1:8",
            "model": "static/bypass",
        }
        with pytest.raises(ValidationError):
            _load(data)

    def test_control_base_url_hygiene(self):
        data = policy_dict()
        data["execution"] = {
            "mode": "direct", "base_url": "http://127.0.0.1:9",
            "model": "devin/swe-2-max",
            "control_base_url": "http://user:pw@127.0.0.1:8",
        }
        with pytest.raises(ValidationError):
            _load(data)


class TestWorkspaceAdmission:
    def test_prepared_requires_config(self):
        data = policy_dict()
        data["workspaces"]["ws-main"] = {
            "repo": "/x", "prepared_worktree": "/x/wt",
        }
        with pytest.raises(ValidationError):
            _load(data)

    def test_config_requires_prepared(self):
        data = policy_dict()
        data["workspaces"]["ws-main"] = {
            "repo": "/x", "runner_execution_config": "/x/cfg.json",
        }
        with pytest.raises(ValidationError):
            _load(data)

    def test_relative_prepared_rejected(self):
        data = policy_dict()
        data["workspaces"]["ws-main"] = {
            "repo": "/x", "prepared_worktree": "rel/wt",
            "runner_execution_config": "/x/cfg.json",
        }
        with pytest.raises(ValidationError):
            _load(data)

    def test_ephemeral_flag_requires_worktree_root(self):
        data = policy_dict()
        ws = dict(data["workspaces"]["ws-main"])
        ws.pop("prepared_worktree")
        ws.pop("runner_execution_config")
        ws["allow_ephemeral_worktree"] = True
        ws.pop("worktree_root", None)
        data["workspaces"]["ws-main"] = ws
        with pytest.raises(ValidationError):
            _load(data)

    def test_runner_effort_pin_values(self):
        data = policy_dict()
        ws = dict(data["workspaces"]["ws-main"])
        ws["runner_effort_pin"] = "low"
        data["workspaces"]["ws-main"] = ws
        p = _load(data)
        assert p.workspaces["ws-main"].runner_effort_pin == "low"
        ws["runner_effort_pin"] = "balanced"   # not an api wire value
        data["workspaces"]["ws-main"] = ws
        with pytest.raises(ValidationError):
            _load(data)


class TestControlIdentity:
    def test_operator_uids_must_be_known_operators(self):
        data = policy_dict()
        data["control"] = {
            "operators": [CURRENT_OS_USER],
            "operator_uids": {"ghost": 1234},
        }
        with pytest.raises(ValidationError):
            _load(data)


class TestVerificationCommands:
    def test_commands_validate_shape(self):
        data = policy_dict()
        data["verification"] = {
            "executables": {"true": "/usr/bin/true",
                            "echo": "/usr/bin/echo"},
            "commands": [["true"], ["echo", "*"]],
        }
        p = _load(data)
        assert p.verification.commands

    def test_commands_empty_entry_rejected(self):
        data = policy_dict()
        data["verification"] = {"commands": [[]]}
        with pytest.raises(ValidationError):
            _load(data)

    def test_commands_wildcard_only_last(self):
        data = policy_dict()
        data["verification"] = {"commands": [["*", "-rf"]]}
        with pytest.raises(ValidationError):
            _load(data)
