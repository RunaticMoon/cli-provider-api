"""Catalog resolution, refresh, and hashing — in-process core coverage."""

from __future__ import annotations

import asyncio

import pytest

from cli_provider_core import (
    AuthorizationError,
    NotFound,
    RunnerUnavailable,
    UnsupportedCapability,
    catalog_view,
    request_hash,
    resolve_model,
)
from cli_provider_core import hash_api_key
from conftest import make_system

pytestmark = pytest.mark.anyio

CATALOGS = [
    {
        "name": "mock-catalog",
        "runner_ref": "runner-1",
        "alias_prefix": "mock/",
        "allow_synthetic_unverified": True,
    }
]

def _row(model_id: str, **extra):
    return {
        "model_id": model_id,
        "display_name": model_id,
        "verification": {"status": "unknown", "source": "fake-fixture"},
        **extra,
    }


MODEL_ROWS = [
    _row("mock-model"),
    _row("mock-effort", effort="selectable", effort_options=["low", "high"]),
    _row(
        "mock-variant",
        effort="model_variant",
        effort_variants={"high": "mock-effort"},
    ),
]


def _overrides(*, exec_grants=("mock-catalog",), read_grants=("mock-catalog",)):
    return {
        "catalogs": CATALOGS,
        "principals": [
            {
                "name": "alpha",
                "key_hash": hash_api_key("secret-alpha"),
                "allowed_presets": ["mock/text"],
                "allowed_workspaces": ["ws-alpha"],
                "allowed_catalogs": list(read_grants),
                "executable_catalogs": list(exec_grants),
                "max_concurrency": 2,
            }
        ],
        "api": {"catalog_refresh_seconds": 0.05},
    }


def _principal(config, name="alpha"):
    return next(p for p in config.principals if p.name == name)


# --------------------------------------------------------------- resolution


async def test_dynamic_alias_resolves_with_exec_grant(tmp_path):
    config, store, registry, _c, control = make_system(tmp_path, **_overrides())
    control.models = MODEL_ROWS
    await registry.refresh()
    binding = resolve_model(
        config=config,
        registry=registry,
        principal=_principal(config),
        alias="mock/mock-effort",
    )
    assert binding.dynamic is True
    assert binding.resolved_model_id == "mock-effort"
    assert binding.preset.runner_ref == "runner-1"
    store.close()


async def test_read_grant_alone_never_executes(tmp_path):
    config, store, registry, _c, control = make_system(
        tmp_path, **_overrides(exec_grants=())
    )
    control.models = MODEL_ROWS
    await registry.refresh()
    with pytest.raises(AuthorizationError):
        resolve_model(
            config=config,
            registry=registry,
            principal=_principal(config),
            alias="mock/mock-effort",
        )
    # ...but the catalog read view still lists it as visible-not-executable.
    view = catalog_view(config, registry, _principal(config))
    entry = {m["model_id"]: m for m in view[0]["models"]}["mock-effort"]
    assert entry["executable"] is False
    store.close()


async def test_unknown_alias_and_invented_id_are_404(tmp_path):
    config, store, registry, _c, control = make_system(tmp_path, **_overrides())
    control.models = MODEL_ROWS
    await registry.refresh()
    for alias in ("mock/never-existed", "agy/gemini-9-ultra", "mock/../x"):
        with pytest.raises(NotFound):
            resolve_model(
                config=config,
                registry=registry,
                principal=_principal(config),
                alias=alias,
            )
    store.close()


async def test_effort_selectable_and_variant_and_unknown(tmp_path):
    config, store, registry, _c, control = make_system(tmp_path, **_overrides())
    control.models = MODEL_ROWS
    await registry.refresh()
    principal = _principal(config)

    sel = resolve_model(
        config=config,
        registry=registry,
        principal=principal,
        alias="mock/mock-effort",
        effort="high",
    )
    assert sel.resolved_model_id == "mock-effort"
    assert sel.effort_support == "selectable"

    var = resolve_model(
        config=config,
        registry=registry,
        principal=principal,
        alias="mock/mock-variant",
        effort="high",
    )
    assert var.resolved_model_id == "mock-effort"
    assert var.effort_support == "model_variant"

    with pytest.raises(UnsupportedCapability):
        resolve_model(
            config=config,
            registry=registry,
            principal=principal,
            alias="mock/mock-model",  # effort unknown
            effort="high",
        )
    with pytest.raises(UnsupportedCapability):
        resolve_model(
            config=config,
            registry=registry,
            principal=principal,
            alias="mock/mock-effort",
            effort="maximum",  # not a declared option
        )
    store.close()


async def test_variant_target_must_be_independently_admitted(tmp_path):
    """A variant mapping to a source-filtered id is rejected, not trusted."""
    config, store, registry, _c, control = make_system(
        tmp_path,
        catalogs=[
            dict(CATALOGS[0], models=["mock-variant"]),  # only the base admitted
        ],
        principals=_overrides()["principals"],
        api={"catalog_refresh_seconds": 0.05},
    )
    control.models = MODEL_ROWS
    await registry.refresh()
    with pytest.raises(UnsupportedCapability):
        resolve_model(
            config=config,
            registry=registry,
            principal=_principal(config),
            alias="mock/mock-variant",
            effort="high",
        )
    store.close()


async def test_static_preset_unchanged_and_effort_enforced(tmp_path):
    config, store, registry, _c, control = make_system(tmp_path, **_overrides())
    # mock-model declared selectable; the static preset can carry effort.
    control.models = [
        _row("mock-model", effort="selectable", effort_options=["low", "high"])
    ]
    await registry.refresh()
    binding = resolve_model(
        config=config,
        registry=registry,
        principal=_principal(config),
        alias="mock/text",
        effort="low",
    )
    assert binding.dynamic is False
    assert binding.resolved_model_id == "mock-model"
    assert binding.reasoning_effort == "low"
    store.close()


# ------------------------------------------------------------------ refresh


async def test_ensure_fresh_is_singleflight_and_ttl_bounded(tmp_path):
    config, store, registry, _c, control = make_system(tmp_path, **_overrides())
    control.models = MODEL_ROWS
    await registry.refresh()
    assert control.discovery_calls == 1

    # Within the TTL a burst of callers triggers no extra discovery pass.
    await asyncio.gather(*[registry.ensure_fresh() for _ in range(8)])
    assert control.discovery_calls == 1

    # Past the TTL, exactly one refresh runs even under concurrency.
    await asyncio.sleep(0.06)
    await asyncio.gather(*[registry.ensure_fresh() for _ in range(8)])
    assert control.discovery_calls == 2
    store.close()


async def test_failed_refresh_keeps_last_snapshot_but_fails_closed(tmp_path):
    config, store, registry, _c, control = make_system(tmp_path, **_overrides())
    control.models = MODEL_ROWS
    await registry.refresh()
    assert registry.runner_health("runner-1").ok

    control.discovery_fail = True
    await registry.ensure_fresh(force=True)
    health = registry.runner_health("runner-1")
    assert health.ok is False
    # The snapshot is kept for display but authorizes nothing.
    assert "mock-effort" in health.model_descriptors
    with pytest.raises(RunnerUnavailable):
        resolve_model(
            config=config,
            registry=registry,
            principal=_principal(config),
            alias="mock/mock-effort",
        )
    # ...and the catalog view marks the source stale, not silently fresh.
    view = catalog_view(config, registry, _principal(config))
    assert view[0]["ok"] is False
    store.close()


# ------------------------------------------------------------------ hashing


def test_request_hash_differentiates_effort(tmp_path):
    base = dict(
        principal="alpha",
        task_id="t1",
        workspace_id="ws-alpha",
        task_policy="default",
        messages=[{"role": "user", "content": "hi"}],
    )
    a = request_hash(**base)
    b = request_hash(**base, reasoning_effort="high")
    c = request_hash(**base, reasoning_effort="low")
    assert a != b != c != a
    # Absent effort keeps the legacy hash (cross-provider replay unchanged).
    assert a == request_hash(**base)


# ------------------------------------------- static variant authority (P1)


VARIANT_ROWS = [
    _row(
        "mock-model",
        effort="model_variant",
        effort_variants={"high": "mock-ungranted"},
    ),
    _row("mock-ungranted"),
]


def _resolve(config, registry, alias, effort=None, principal_name="alpha"):
    return resolve_model(
        config=config,
        registry=registry,
        principal=_principal(config, principal_name),
        alias=alias,
        effort=effort,
    )


async def test_static_variant_requires_independent_authorization(tmp_path):
    """Catalog membership + driver executable are not the principal's grant:
    a variant to an ungranted model id is refused before any run."""
    config, store, registry, _c, control = make_system(tmp_path)
    control.models = VARIANT_ROWS
    await registry.refresh()
    with pytest.raises(UnsupportedCapability):
        _resolve(config, registry, "mock/text", effort="high")
    store.close()


async def test_static_variant_authorized_via_held_preset(tmp_path):
    """The same variant is admitted when the principal holds an enabled
    preset binding the target id on the same runner and task policy."""
    config, store, registry, _c, control = make_system(
        tmp_path,
        presets=[
            {
                "alias": "mock/text",
                "runner_ref": "runner-1",
                "model_id": "mock-model",
                "allow_synthetic_unverified": True,
            },
            {
                "alias": "mock/hi",
                "runner_ref": "runner-1",
                "model_id": "mock-ungranted",
                "task_policy": "text",
                "allow_synthetic_unverified": True,
            },
        ],
        principals=[
            {
                "name": "alpha",
                "key_hash": hash_api_key("secret-alpha"),
                "allowed_presets": ["mock/text", "mock/hi"],
                "allowed_workspaces": ["ws-alpha"],
                "max_concurrency": 2,
            }
        ],
    )
    control.models = VARIANT_ROWS
    await registry.refresh()
    binding = _resolve(config, registry, "mock/text", effort="high")
    assert binding.resolved_model_id == "mock-ungranted"
    store.close()


async def test_static_variant_denied_by_policy_or_grant_gaps(tmp_path):
    """Target presets on a different task policy, or not granted to the
    principal, or disabled, never authorize the variant."""
    presets = [
        {
            "alias": "mock/text",
            "runner_ref": "runner-1",
            "model_id": "mock-model",
            "allow_synthetic_unverified": True,
        },
        {
            "alias": "mock/hi-review",  # different task policy
            "runner_ref": "runner-1",
            "model_id": "mock-ungranted",
            "task_policy": "review",
            "allow_synthetic_unverified": True,
        },
    ]
    config, store, registry, _c, control = make_system(
        tmp_path,
        presets=presets,
        principals=[
            {
                "name": "alpha",
                "key_hash": hash_api_key("secret-alpha"),
                "allowed_presets": ["mock/text", "mock/hi-review"],
                "allowed_workspaces": ["ws-alpha"],
                "max_concurrency": 2,
            }
        ],
    )
    control.models = VARIANT_ROWS
    await registry.refresh()
    # Held target preset, but policy differs from the source preset.
    with pytest.raises(UnsupportedCapability):
        _resolve(config, registry, "mock/text", effort="high")
    store.close()

    config, store, registry, _c, control = make_system(
        tmp_path,
        presets=presets + [
            {
                "alias": "mock/hi",
                "runner_ref": "runner-1",
                "model_id": "mock-ungranted",
                "task_policy": "text",
                "allow_synthetic_unverified": True,
            }
        ],
        principals=[
            {
                "name": "alpha",
                # mock/hi matches policy+runner but is not granted.
                "key_hash": hash_api_key("secret-alpha"),
                "allowed_presets": ["mock/text"],
                "allowed_workspaces": ["ws-alpha"],
                "max_concurrency": 2,
            }
        ],
    )
    control.models = VARIANT_ROWS
    await registry.refresh()
    with pytest.raises(UnsupportedCapability):
        _resolve(config, registry, "mock/text", effort="high")
    store.close()


async def test_static_variant_authorized_via_catalog_exec_grant(tmp_path):
    """An executable-catalog grant on the same runner whose source policy
    admits the target is an equivalent authority for the variant."""
    config, store, registry, _c, control = make_system(
        tmp_path,
        **_overrides(),
    )
    control.models = VARIANT_ROWS
    await registry.refresh()
    binding = _resolve(config, registry, "mock/text", effort="high")
    assert binding.resolved_model_id == "mock-ungranted"

    # ...but a source cost/model filter on that grant still applies.
    config2, store2, registry2, _c2, control2 = make_system(
        tmp_path / "b",
        catalogs=[dict(CATALOGS[0], cost_tiers=["free"])],
        principals=_overrides()["principals"],
        api={"catalog_refresh_seconds": 0.05},
    )
    control2.models = [
        _row(
            "mock-model",
            effort="model_variant",
            effort_variants={"high": "mock-ungranted"},
            cost_tier="free",
        ),
        _row("mock-ungranted", cost_tier="paid"),  # source filter denies it
    ]
    await registry2.refresh()
    with pytest.raises(UnsupportedCapability):
        _resolve(config2, registry2, "mock/text", effort="high")
    store.close()
    store2.close()


async def test_dynamic_variant_requires_the_same_run_gate(tmp_path):
    """An exact allowed_presets alias grant (no source-wide exec grant) must
    not effort-hop to another source-admitted model id."""
    config, store, registry, _c, control = make_system(
        tmp_path,
        catalogs=CATALOGS,
        principals=[
            {
                "name": "alpha",
                "key_hash": hash_api_key("secret-alpha"),
                # Only the base alias is granted; no executable_catalogs.
                "allowed_presets": ["mock/text", "mock/mock-variant"],
                "allowed_workspaces": ["ws-alpha"],
                "max_concurrency": 2,
            }
        ],
        api={"catalog_refresh_seconds": 0.05},
    )
    control.models = MODEL_ROWS
    await registry.refresh()
    # The base alias itself resolves fine.
    base = _resolve(config, registry, "mock/mock-variant")
    assert base.resolved_model_id == "mock-variant"
    # The variant target alias is not granted → refused, not admitted by the
    # source policy alone.
    with pytest.raises(UnsupportedCapability):
        _resolve(config, registry, "mock/mock-variant", effort="high")

    # With the source-wide grant the same request resolves.
    config2, store2, registry2, _c2, control2 = make_system(
        tmp_path / "c", **_overrides()
    )
    control2.models = MODEL_ROWS
    await registry2.refresh()
    ok = _resolve(config2, registry2, "mock/mock-variant", effort="high")
    assert ok.resolved_model_id == "mock-effort"
    store.close()
    store2.close()
