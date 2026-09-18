"""Registry contract validation: SDK schemas, verification and capabilities."""

from __future__ import annotations

from typing import Any

from cli_provider_core import RunnerRegistry
from cli_provider_sdk import SDK_VERSION

from conftest import base_config


def _capabilities(**overrides: Any) -> dict[str, Any]:
    caps = {
        "streaming": "native",
        "sessions": "none",
        "roles": "serialized",
        "structured_output": "none",
        "external_tool_calls": False,
        "internal_tools": False,
        "vision": False,
        "workspace_write": False,
        "web_search": False,
        "usage": "unknown",
    }
    caps.update(overrides)
    return caps


def _manifest(
    driver_id: str, version: str, *, synthetic: bool = True, **overrides: Any
) -> dict[str, Any]:
    data = {
        "driver_id": driver_id,
        "name": "Mock",
        "version": version,
        "sdk_version": SDK_VERSION,
        "protocol_family": "mock",
        "supported_transports": ["stdio"],
        "synthetic": synthetic,
    }
    data.update(overrides)
    return data


def _model(verification_status: str = "unknown") -> dict[str, Any]:
    return {
        "model_id": "mock-model",
        "display_name": "Mock Model",
        "verification": {"status": verification_status, "source": "fixture"},
    }


class PayloadSession:
    def __init__(self, cfg, payloads: dict[str, Any]) -> None:
        self.cfg = cfg
        self.payloads = payloads

    async def manifest(self) -> dict[str, Any]:
        return self.payloads["manifest"]

    async def probe(self) -> dict[str, Any]:
        return self.payloads["probe"]

    async def discover_models(self) -> list[dict[str, Any]]:
        return self.payloads["models"]

    async def aclose(self) -> None:
        return None


def make_registry(tmp_path, payloads: dict[str, Any], **overrides) -> RunnerRegistry:
    config = base_config(tmp_path, **overrides)

    def factory(cfg):
        resolved = dict(payloads)
        resolved.setdefault("manifest", _manifest(cfg.driver_id, cfg.driver_version))
        resolved.setdefault(
            "probe",
            {
                "ok": True,
                "driver_id": cfg.driver_id,
                "driver_version": cfg.driver_version,
                "cli_version": None,
                "capabilities": _capabilities(),
            },
        )
        resolved.setdefault("models", [_model()])
        return PayloadSession(cfg, resolved)

    return RunnerRegistry(config, session_factory=factory)


async def test_valid_synthetic_opt_in_available_but_not_really_verified(tmp_path):
    registry = make_registry(tmp_path, {})
    await registry.refresh()
    health = registry.preset_health("mock/text")
    assert health.verified is True
    assert health.real_verification is False
    assert "synthetic" in health.detail
    assert health.capabilities["streaming"] == "native"
    assert health.capabilities["task_policy"] == "text"
    assert registry.preset_available("mock/text") is True


async def test_malformed_manifest_is_refused(tmp_path):
    registry = make_registry(
        tmp_path, {"manifest": {"driver_id": "mock", "name": "Mock"}}
    )
    await registry.refresh()
    assert registry.runner_health("runner-1").ok is False
    assert "schema validation" in registry.runner_health("runner-1").detail
    assert registry.preset_available("mock/text") is False


async def test_malformed_probe_is_refused(tmp_path):
    registry = make_registry(
        tmp_path,
        {
            "probe": {
                "ok": True,
                "driver_id": "mock",
                "driver_version": "0.1.0",
                # capabilities missing entirely
            }
        },
    )
    await registry.refresh()
    assert registry.runner_health("runner-1").ok is False
    assert registry.preset_available("mock/text") is False


async def test_malformed_model_descriptor_is_refused(tmp_path):
    registry = make_registry(
        tmp_path,
        {"models": [{"model_id": "mock-model"}, "not-an-object"]},
    )
    await registry.refresh()
    assert registry.runner_health("runner-1").ok is False
    assert registry.preset_available("mock/text") is False


async def test_undeclared_model_is_not_available(tmp_path):
    registry = make_registry(tmp_path, {"models": []})
    await registry.refresh()
    assert registry.runner_health("runner-1").ok is True
    assert registry.preset_available("mock/text") is False


async def test_streaming_none_capability_is_refused(tmp_path):
    registry = make_registry(
        tmp_path,
        {
            "probe": {
                "ok": True,
                "driver_id": "mock",
                "driver_version": "0.1.0",
                "cli_version": None,
                "capabilities": _capabilities(streaming="none"),
            }
        },
    )
    await registry.refresh()
    assert registry.preset_available("mock/text") is False
    assert "streaming=none" in registry.preset_health("mock/text").detail


async def test_roles_unsupported_capability_is_refused(tmp_path):
    registry = make_registry(
        tmp_path,
        {
            "probe": {
                "ok": True,
                "driver_id": "mock",
                "driver_version": "0.1.0",
                "cli_version": None,
                "capabilities": _capabilities(roles="unsupported"),
            }
        },
    )
    await registry.refresh()
    assert registry.preset_available("mock/text") is False


async def test_native_driver_requires_passed_verification(tmp_path):
    native = _manifest("mock", "0.1.0", synthetic=False)
    unknown = make_registry(
        tmp_path, {"manifest": native, "models": [_model("unknown")]}
    )
    await unknown.refresh()
    assert unknown.preset_available("mock/text") is False

    passed = make_registry(
        tmp_path, {"manifest": native, "models": [_model("passed")]}
    )
    await passed.refresh()
    assert passed.preset_available("mock/text") is True
    assert passed.preset_health("mock/text").real_verification is True


async def test_synthetic_self_reported_passed_is_never_real_verification(tmp_path):
    """Finding 1: a synthetic driver's self-reported `passed` is never real."""
    registry = make_registry(tmp_path, {"models": [_model("passed")]})
    await registry.refresh()
    health = registry.preset_health("mock/text")
    assert health.verified is True
    assert health.real_verification is False
    assert health.capabilities["real_verification"] is False
    assert registry.runner_health("runner-1").synthetic is True
    assert "synthetic" in health.detail


async def test_runtime_fallback_is_schema_valid_and_conservative(tmp_path):
    """Finding 3: a session with no `runtime()` still yields a valid value."""
    registry = make_registry(tmp_path, {})  # PayloadSession declares no runtime()
    await registry.refresh()
    health = registry.runner_health("runner-1")
    assert health.ok is True
    assert health.max_parallel_runs == 1
    assert health.max_queue >= 1
    assert registry.runner_cleanup_seconds("runner-1") > 0


async def test_synthetic_unknown_without_opt_in_is_refused(tmp_path):
    registry = make_registry(
        tmp_path,
        {},
        presets=[
            {"alias": "mock/text", "runner_ref": "runner-1", "model_id": "mock-model"},
            {
                "alias": "mock/text-beta",
                "runner_ref": "runner-1",
                "model_id": "mock-model",
            },
            {
                "alias": "mock/review",
                "runner_ref": "runner-1",
                "model_id": "mock-model",
                "task_policy": "review",
            },
        ],
    )
    await registry.refresh()
    assert registry.preset_available("mock/text") is False
    assert "not 'passed'" in registry.preset_health("mock/text").detail
