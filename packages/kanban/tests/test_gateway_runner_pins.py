"""A logical combo name must not be used as a Runner preset/model pin."""
import json

import pytest

from cli_provider_kanban.dispatch import _candidate_models
from cli_provider_kanban.policy import Policy
from cli_provider_kanban.worktree import validate_runner_binding
from cli_provider_runner.execution_config import load_execution_config


def policy_for(backend):
    return Policy.model_validate({
        'schema_version': 1, 'policy_version': 'pin-test',
        'scope': {'assignee': 'jev-native', 'statuses': ['ready']},
        'capabilities': ['code'],
        'backends': [dict(backend, id='worker', enabled=True, capabilities={'code': True})],
        'routes': {'worker.code.standard': {'candidates': ['worker']}},
    })


@pytest.mark.parametrize('backend,descriptor', [
    ({'kind': 'devin', 'transport': 'native', 'driver': 'devin',
      'model': 'swe-2-max', 'preset': 'devin/swe-2-max'}, 'swe-2-max'),
    ({'kind': 'bai', 'transport': 'api', 'driver': 'hermes-api',
      'model': 'deepseek-v4.1-flash', 'preset': 'bai/deepseek-v4.1-flash'}, 'bai:deepseek-v4.1-flash'),
    ({'kind': 'commandcode', 'transport': 'api', 'driver': 'hermes-api',
      'model': 'deepseek/deepseek-v4.1-flash', 'preset': 'commandcode/deepseek-v4.1-flash'}, 'commandcode:deepseek-v4.1-flash'),
])
def test_candidates_use_actual_runner_model_alias(backend, descriptor):
    policy = policy_for(backend)
    route = policy.routes['worker.code.standard']
    assert _candidate_models(policy, route, 'code') == [descriptor]


def test_gateway_binding_validates_concrete_presets_not_combo(tmp_path):
    tmp_path.chmod(0o700)
    root = tmp_path / 'worktree'
    root.mkdir()
    config = tmp_path / 'execution.json'
    config.write_text(json.dumps({'version': 1, 'workspaces': {'toy': {
        'root': str(root), 'allowed_actions': [],
        'allowed_presets': ['devin/swe-2-max'], 'allowed_models': ['swe-2-max'],
    }}}))
    config.chmod(0o600)
    # Exercise the real Runner loader with the SAME protected bytes.
    actual = load_execution_config(str(config)).binding_for('toy')
    assert actual.allowed_presets == ['devin/swe-2-max']
    assert actual.allowed_models == ['swe-2-max']
    result = validate_runner_binding(
        config_path=str(config), workspace_id='toy', prepared_path=str(root),
        submitted_model='jev.worker.code.standard',
        candidate_presets=['devin/swe-2-max'], candidate_models=['swe-2-max'],
    )
    assert result['root'] == str(root)
