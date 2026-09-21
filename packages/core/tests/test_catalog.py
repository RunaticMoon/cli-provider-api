"""Catalog resolution, refresh, and hashing — in-process core coverage."""

from __future__ import annotations

import asyncio

import pytest

from cli_provider_core import (
    AuthorizationError,
    NotFound,
    RunnerUnavailable,
    UnsupportedCapability,
    alias_refresh_refs,
    catalog_refresh_refs,
    catalog_view,
    models_refresh_refs,
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


# --------------------------------------------------- duplicate model IDs


async def test_conflicting_duplicate_model_id_fails_closed(tmp_path):
    """A catalog reporting the same id twice with conflicting rows must not
    be resolved by ordering: the runner fails verification entirely, so no
    cost/executable/effort row can be picked by position."""
    config, store, registry, _c, control = make_system(tmp_path, **_overrides())
    control.models = [
        _row("mock-model"),
        _row("mock-effort", cost_tier="free", executable=True),
        _row("mock-effort", cost_tier="paid", executable=False),  # conflict
    ]
    await registry.refresh()
    health = registry.runner_health("runner-1")
    assert health.ok is False
    assert "duplicate" in health.detail.lower()
    with pytest.raises(RunnerUnavailable):
        resolve_model(
            config=config,
            registry=registry,
            principal=_principal(config),
            alias="mock/mock-effort",
        )
    # The catalog view marks the source failed rather than advertising a
    # freshly-picked row.
    view = catalog_view(config, registry, _principal(config))
    assert view[0]["ok"] is False
    store.close()


async def test_duplicate_refresh_preserves_prior_safe_state(tmp_path):
    """A malformed (duplicate) refresh after a healthy one must not silently
    replace admission with an ordering pick: the source goes stale/unavailable
    and execution fails closed."""
    config, store, registry, _c, control = make_system(tmp_path, **_overrides())
    control.models = MODEL_ROWS
    await registry.refresh()
    assert registry.runner_health("runner-1").ok

    control.models = MODEL_ROWS + [
        _row("mock-effort", cost_tier="paid", executable=True)  # conflict
    ]
    await registry.refresh()
    assert registry.runner_health("runner-1").ok is False
    with pytest.raises(RunnerUnavailable):
        resolve_model(
            config=config,
            registry=registry,
            principal=_principal(config),
            alias="mock/mock-effort",
        )
    store.close()


async def test_empty_catalog_remains_legitimate(tmp_path):
    """An empty catalog is not malformed: the runner stays verified and lists
    zero models."""
    config, store, registry, _c, control = make_system(tmp_path, **_overrides())
    control.models = []
    await registry.refresh()
    assert registry.runner_health("runner-1").ok
    with pytest.raises(NotFound):
        resolve_model(
            config=config,
            registry=registry,
            principal=_principal(config),
            alias="mock/mock-effort",
        )
    store.close()


# ------------------------------ r2: catalog-grant task-policy coherence (1)


def _variant_system(tmp_path, *, source_policy="text", grants=None):
    """Static mock/text (policy 'text') whose mock-model maps effort 'high'
    to the catalog-only mock-ungranted; the catalog source policy varies."""
    source = dict(
        CATALOGS[0], task_policy=source_policy,
    )
    overrides = _overrides()
    overrides["catalogs"] = [source]
    if grants is not None:
        overrides["principals"][0].update(grants)
    config, store, registry, _c, control = make_system(tmp_path, **overrides)
    control.models = VARIANT_ROWS
    return config, store, registry


async def test_static_variant_catalog_grant_must_match_task_policy(tmp_path):
    """A catalog source under a DIFFERENT task policy cannot lend its
    execution grant to the static binding: authority never composes across
    policies."""
    # Broad source grant, differing policy -> denied.
    config, store, registry = _variant_system(tmp_path, source_policy="review")
    await registry.refresh()
    with pytest.raises(UnsupportedCapability):
        _resolve(config, registry, "mock/text", effort="high")
    store.close()

    # Exact-alias grant, differing policy -> still denied.
    config, store, registry = _variant_system(
        tmp_path / "b",
        source_policy="review",
        grants={
            "executable_catalogs": [],
            "allowed_presets": ["mock/text", "mock/mock-ungranted"],
        },
    )
    await registry.refresh()
    with pytest.raises(UnsupportedCapability):
        _resolve(config, registry, "mock/text", effort="high")
    store.close()


async def test_static_variant_catalog_grant_same_policy_authorized(tmp_path):
    """Positive controls: same task policy authorizes via both the broad
    source grant and an exact dynamic alias grant."""
    config, store, registry = _variant_system(tmp_path, source_policy="text")
    await registry.refresh()
    binding = _resolve(config, registry, "mock/text", effort="high")
    assert binding.resolved_model_id == "mock-ungranted"
    assert binding.preset.task_policy == "text"
    store.close()

    config, store, registry = _variant_system(
        tmp_path / "b",
        source_policy="text",
        grants={
            "executable_catalogs": [],
            "allowed_presets": ["mock/text", "mock/mock-ungranted"],
        },
    )
    await registry.refresh()
    binding = _resolve(config, registry, "mock/text", effort="high")
    assert binding.resolved_model_id == "mock-ungranted"
    store.close()


# -------------------------------------- r2: static alias shadow marker (2)


def _shadow_system(tmp_path, **kwargs):
    overrides = _overrides()
    overrides.update(kwargs)
    config, store, registry, _c, control = make_system(tmp_path, **overrides)
    return config, store, registry, control


async def test_catalog_view_marks_shadowed_row_non_executable(tmp_path):
    """A dynamic row whose alias equals a static preset resolves to the
    static authority — the catalog view must never advertise it as
    executable under the source grant."""
    config, store, registry, control = _shadow_system(tmp_path)
    control.models = MODEL_ROWS + [_row("text")]  # alias 'mock/text' collides
    await registry.refresh()

    view = catalog_view(config, registry, _principal(config))
    by_id = {m["model_id"]: m for m in view[0]["models"]}
    shadowed = by_id["text"]
    assert shadowed["alias"] == "mock/text"
    assert shadowed["executable"] is False
    assert "shadowed by a static preset" in shadowed["rejection"]

    # The resolution itself still binds the static preset.
    binding = _resolve(config, registry, "mock/text")
    assert binding.dynamic is False
    assert binding.preset.model_id == "mock-model"
    # ...and a non-colliding dynamic row stays executable.
    assert by_id["mock-effort"]["executable"] is True
    store.close()


async def test_disabled_static_preset_still_shadows(tmp_path):
    """Shadowing follows alias identity, not enabled state: a disabled
    static preset still owns the alias, so the dynamic row cannot claim it."""
    presets = [
        {
            "alias": "mock/text",
            "runner_ref": "runner-1",
            "model_id": "mock-model",
            "allow_synthetic_unverified": True,
        },
        {
            "alias": "mock/ghost",
            "runner_ref": "runner-1",
            "model_id": "mock-model",
            "enabled": False,
            "allow_synthetic_unverified": True,
        },
    ]
    overrides = _overrides()
    overrides["presets"] = presets
    overrides["principals"][0]["allowed_presets"] = ["mock/text", "mock/ghost"]
    config, store, registry, _c, control = make_system(tmp_path, **overrides)
    control.models = MODEL_ROWS + [_row("ghost")]
    await registry.refresh()
    view = catalog_view(config, registry, _principal(config))
    by_id = {m["model_id"]: m for m in view[0]["models"]}
    assert by_id["ghost"]["executable"] is False
    assert "shadowed by a static preset" in by_id["ghost"]["rejection"]
    # Resolution never falls through to the dynamic row.
    with pytest.raises(RunnerUnavailable):
        _resolve(config, registry, "mock/ghost")
    store.close()


# -------------------------------------- r2: scoped per-runner refresh (3)

from cli_provider_core import RunnerRegistry
from conftest import FakeControl, FakeSession


def _two_runner_system(tmp_path, *, principals=None, catalogs=None):
    """Two counted fake runners: controls[i].discovery_calls is the RPC
    count for that runner's verification pass."""
    overrides = _overrides()
    overrides["runners"] = [
        {
            "instance_id": "runner-1",
            "driver_id": "mock",
            "driver_version": "0.1.0",
            "distribution": "cli-driver-mock",
            "socket_path": str(tmp_path / "r1.sock"),
        },
        {
            "instance_id": "runner-2",
            "driver_id": "mock",
            "driver_version": "0.1.0",
            "distribution": "cli-driver-mock",
            "socket_path": str(tmp_path / "r2.sock"),
        },
    ]
    overrides["catalogs"] = catalogs or [
        {
            "name": "src-a",
            "runner_ref": "runner-1",
            "alias_prefix": "mock/",
            "allow_synthetic_unverified": True,
        },
        {
            "name": "src-b",
            "runner_ref": "runner-2",
            "alias_prefix": "aux/",
            "allow_synthetic_unverified": True,
        },
    ]
    if principals is not None:
        overrides["principals"] = principals
    else:
        overrides["principals"][0].update(
            {
                "allowed_catalogs": ["src-a", "src-b"],
                "executable_catalogs": ["src-a", "src-b"],
            }
        )
    config, store, _registry, _c, _shared = make_system(tmp_path, **overrides)
    controls = {"runner-1": FakeControl(), "runner-2": FakeControl()}
    registry = RunnerRegistry(
        config,
        session_factory=lambda cfg: FakeSession(cfg, controls[cfg.instance_id]),
    )
    for c in controls.values():
        c.models = MODEL_ROWS
    return config, store, registry, controls


async def test_scoped_refresh_contacts_only_named_runners(tmp_path):
    config, store, registry, controls = _two_runner_system(tmp_path)
    await registry.ensure_fresh(runner_refs={"runner-1"})
    assert controls["runner-1"].discovery_calls == 1
    assert controls["runner-2"].discovery_calls == 0
    # Runner-2 was never attempted: no freshness mark, snapshot untouched.
    assert registry.runner_health("runner-2").observed_monotonic is None
    store.close()


async def test_partial_refresh_does_not_mark_other_runner_fresh(tmp_path):
    """After A refreshes, B is still stale and gets its own pass."""
    config, store, registry, controls = _two_runner_system(tmp_path)
    await registry.ensure_fresh(runner_refs={"runner-1"})
    await registry.ensure_fresh(runner_refs={"runner-2"})
    assert controls["runner-1"].discovery_calls == 1
    assert controls["runner-2"].discovery_calls == 1
    store.close()


async def test_scoped_refresh_ttl_singleflight_and_force(tmp_path):
    """Per-runner TTL: a second in-window call is a no-op; concurrent calls
    share the guard; force bypasses the TTL for the named runner only."""
    config, store, registry, controls = _two_runner_system(tmp_path)
    await registry.ensure_fresh(runner_refs={"runner-1"})
    await registry.ensure_fresh(runner_refs={"runner-1"})
    assert controls["runner-1"].discovery_calls == 1

    await asyncio.gather(
        registry.ensure_fresh(runner_refs={"runner-1"}),
        registry.ensure_fresh(runner_refs={"runner-1"}),
    )
    assert controls["runner-1"].discovery_calls == 1

    await registry.ensure_fresh(runner_refs={"runner-1"}, force=True)
    assert controls["runner-1"].discovery_calls == 2
    assert controls["runner-2"].discovery_calls == 0
    store.close()


async def test_global_refresh_still_covers_all_runners(tmp_path):
    """The internal startup/global path is preserved: no scope -> everyone."""
    config, store, registry, controls = _two_runner_system(tmp_path)
    await registry.ensure_fresh()
    assert controls["runner-1"].discovery_calls == 1
    assert controls["runner-2"].discovery_calls == 1
    store.close()


# ------------------------------------------------- refresh-scope helpers


def test_alias_refresh_refs_scope(tmp_path):
    """The request-scoped refs mirror resolution: granted static -> its
    runner; granted dynamic -> the source runner; denied/unknown -> empty."""
    config, store, registry, _controls = _two_runner_system(tmp_path)
    alpha = _principal(config)
    assert alias_refresh_refs(config, alpha, "mock/text") == {"runner-1"}
    assert alias_refresh_refs(config, alpha, "mock/mock-effort") == {"runner-1"}
    assert alias_refresh_refs(config, alpha, "aux/mock-effort") == {"runner-2"}
    # Unknown alias, denied dynamic alias, denied static -> no runner work.
    assert alias_refresh_refs(config, alpha, "bogus/x") == set()
    assert alias_refresh_refs(config, alpha, "mock/text-beta") == set()
    # driver_scope filters statically and dynamically.
    assert (
        alias_refresh_refs(config, alpha, "mock/text", driver_scope="agy")
        == set()
    )
    assert (
        alias_refresh_refs(config, alpha, "aux/mock-effort", driver_scope="mock")
        == {"runner-2"}
    )
    store.close()


def test_catalog_and_models_refresh_refs_scope(tmp_path):
    config, store, registry, _controls = _two_runner_system(
        tmp_path,
        principals=[
            {
                "name": "alpha",
                "key_hash": hash_api_key("secret-alpha"),
                "allowed_presets": ["mock/text"],
                "allowed_workspaces": ["ws-alpha"],
                "allowed_catalogs": ["src-a"],   # read-only A
                "executable_catalogs": ["src-b"],  # exec B (implies read)
                "max_concurrency": 2,
            }
        ],
    )
    alpha = _principal(config)
    assert catalog_refresh_refs(config, alpha) == {"runner-1", "runner-2"}
    assert catalog_refresh_refs(config, alpha, driver_scope="nope") == set()
    # /v1/models needs exec-capable sources + held static presets, not
    # read-only ones: runner-2 (exec B) + runner-1 (static mock/text).
    assert models_refresh_refs(config, alpha) == {"runner-1", "runner-2"}
    store.close()

    # A key with only a static grant and no catalogs refreshes just its
    # preset runner.
    config2, store2, _r2, _c2 = _two_runner_system(
        tmp_path / "b",
        principals=[
            {
                "name": "alpha",
                "key_hash": hash_api_key("secret-alpha"),
                "allowed_presets": ["mock/text"],
                "allowed_workspaces": ["ws-alpha"],
                "max_concurrency": 2,
            }
        ],
    )
    alpha2 = _principal(config2)
    assert catalog_refresh_refs(config2, alpha2) == set()
    assert models_refresh_refs(config2, alpha2) == {"runner-1"}
    store2.close()
