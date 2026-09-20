"""Owner locking must not leak on startup failure or alter hardlinks."""
import os
import pytest
from cli_provider_api.app import create_app
from cli_provider_api.ownerlock import (
    ApiOwnerError, acquire_store_owner_lock, release_store_owner_lock,
)
from cli_provider_core import OperatorConfig
from conftest import MockSystem


async def test_startup_refresh_failure_releases_api_owner(tmp_path, monkeypatch):
    config = OperatorConfig.model_validate(MockSystem(str(tmp_path))._config())
    app = create_app(config)

    async def fail_refresh():
        raise RuntimeError('injected refresh failure')

    monkeypatch.setattr(app.state.registry, 'refresh', fail_refresh)
    with pytest.raises(RuntimeError, match='injected refresh failure'):
        async with app.router.lifespan_context(app):
            pytest.fail('startup unexpectedly succeeded')
    fd = acquire_store_owner_lock(config.db_path())
    release_store_owner_lock(fd)


def test_hardlinked_lock_does_not_truncate_unrelated_file(tmp_path):
    db = str(tmp_path / 'core.db')
    target = tmp_path / 'sentinel'
    target.write_text('must remain unchanged')
    os.link(target, db + '.api-owner.lock')
    with pytest.raises(ApiOwnerError):
        fd = acquire_store_owner_lock(db)
        release_store_owner_lock(fd)
    assert target.read_text() == 'must remain unchanged'
