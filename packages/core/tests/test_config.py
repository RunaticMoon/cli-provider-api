import pytest
from pydantic import ValidationError

from cli_provider_core import OperatorConfig, hash_api_key, load_config, verify_api_key

from conftest import base_config


def test_valid_config_loads(tmp_path):
    config = base_config(tmp_path)
    assert config.schema_version == 1
    assert config.preset_map()["mock/text"].model_id == "mock-model"
    assert config.db_path().endswith("core.db")


def test_plaintext_key_is_rejected(tmp_path):
    with pytest.raises(ValidationError):
        base_config(
            tmp_path,
            principals=[
                {
                    "name": "alpha",
                    "key_hash": "not-a-hash",
                    "allowed_presets": ["mock/text"],
                    "allowed_workspaces": ["ws-alpha"],
                    "max_concurrency": 1,
                }
            ],
        )


def test_missing_required_binding_is_an_error(tmp_path):
    with pytest.raises(ValidationError):
        base_config(tmp_path, workspaces=[])
    with pytest.raises(ValidationError):
        base_config(tmp_path, principals=[])
    with pytest.raises(ValidationError):
        base_config(tmp_path, presets=[])


def test_preset_referencing_unknown_runner_is_an_error(tmp_path):
    with pytest.raises(ValidationError):
        base_config(
            tmp_path,
            presets=[
                {"alias": "mock/text", "runner_ref": "nope", "model_id": "mock-model"}
            ],
        )


def test_principal_unknown_preset_or_workspace_is_an_error(tmp_path):
    with pytest.raises(ValidationError):
        base_config(
            tmp_path,
            principals=[
                {
                    "name": "alpha",
                    "key_hash": hash_api_key("x"),
                    "allowed_presets": ["missing/alias"],
                    "allowed_workspaces": ["ws-alpha"],
                    "max_concurrency": 1,
                }
            ],
        )
    with pytest.raises(ValidationError):
        base_config(
            tmp_path,
            principals=[
                {
                    "name": "alpha",
                    "key_hash": hash_api_key("x"),
                    "allowed_presets": ["mock/text"],
                    "allowed_workspaces": ["missing-ws"],
                    "max_concurrency": 1,
                }
            ],
        )


def test_no_default_model_substitution_on_unknown_field(tmp_path):
    with pytest.raises(ValidationError):
        base_config(tmp_path, default_model="mock/text")


def test_yaml_and_json_load_equally(tmp_path):
    config = base_config(tmp_path)
    yaml_path = tmp_path / "config.yaml"
    json_path = tmp_path / "config.json"
    payload = config.model_dump(mode="json")
    import json

    import yaml

    yaml_path.write_text(yaml.safe_dump(payload))
    json_path.write_text(json.dumps(payload))
    assert load_config(str(yaml_path)) == config
    assert load_config(str(json_path)) == config


def test_key_hash_roundtrip():
    digest = hash_api_key("secret-alpha")
    assert digest.startswith("sha256:")
    assert verify_api_key("secret-alpha", digest)
    assert not verify_api_key("wrong", digest)


def test_config_rejects_extra_fields(tmp_path):
    with pytest.raises(ValidationError):
        OperatorConfig.model_validate(
            {**base_config(tmp_path).model_dump(mode="json"), "unexpected": 1}
        )


def test_shipped_example_config_is_valid():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    config = load_config(str(root / "config.example.yaml"))
    assert config.schema_version == 1
    assert "mock/text" in config.preset_map()
    # Placeholder hashes are not real credentials.
    assert all(p.key_hash.startswith("sha256:") for p in config.principals)
