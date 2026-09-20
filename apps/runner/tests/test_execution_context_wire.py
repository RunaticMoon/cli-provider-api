"""Integration gate: typed execution context must traverse the real Runner.
No provider inference; the stock mock distribution handles the run.
"""
import pytest
from pydantic import ValidationError
from cli_provider_runner.protocol import RunParams
from cli_provider_runner.client import RunnerClient
from conftest import run_params

CONTEXT = {
    'task_revision': 'rev-3',
    'base_revision': '8ca6569820f1cc7563bd7fa2769de71fbec25f6b',
    'route': 'worker.coding.standard',
    'policy_version': 'v1',
}


def test_typed_context_reaches_normalized_driver_request():
    params = RunParams.model_validate(run_params(execution=CONTEXT))
    assert params.to_driver_request().execution.model_dump() == CONTEXT
    assert params.to_driver_request().run_id == 'run-1'


async def test_real_runner_accepts_context_without_reinterpreting_it(runner_factory):
    runner = runner_factory()
    client = await RunnerClient.connect(runner.socket_path)
    try:
        events = [e async for e in client.run(run_params(execution=CONTEXT))]
        assert client.last_run_response.ok
        assert client.last_run_response.result['status'] == 'completed'
        assert events[-1].event.kind == 'run.completed'
    finally:
        await client.aclose()


@pytest.mark.parametrize('extra', [{'cwd':'/tmp'}, {'backend':'codex'}, {'run_id':'overridden'}])
def test_execution_context_cannot_select_runtime_authority(extra):
    with pytest.raises(ValidationError):
        RunParams.model_validate(run_params(execution={**CONTEXT, **extra}))
