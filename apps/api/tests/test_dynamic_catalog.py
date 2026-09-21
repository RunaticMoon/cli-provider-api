"""Dynamic catalog: operator-owned discovery sources vs run authorization.

These tests boot the real API + real Runner subprocesses against the synthetic
mock driver (see conftest.py). They prove a driver-discovered model with no
per-model preset becomes visible through the authenticated catalog endpoint
when the principal is permitted the catalog source — and that visibility alone
never executes anything. Reasoning-effort carriers, TTL refresh, and the
idempotency boundary are exercised end to end.
"""

from __future__ import annotations

import json
import time

from cli_provider_core import hash_api_key


def _catalog_config(
    *,
    executable_catalogs=None,
    allowed_catalogs=("mock-catalog",),
    refresh_seconds: float | None = None,
    source_overrides: dict | None = None,
) -> dict:
    """Config overlay: one mock catalog source + a principal permitted to read it."""
    principals = [
        {
            "name": "alpha",
            "key_hash": hash_api_key("local-alpha-key"),
            "allowed_presets": ["mock/text"],
            "allowed_workspaces": ["ws-alpha"],
            "allowed_catalogs": list(allowed_catalogs),
            "executable_catalogs": list(executable_catalogs or []),
            "max_concurrency": 2,
        }
    ]
    source = {
        "name": "mock-catalog",
        "runner_ref": "runner-1",
        "alias_prefix": "mock/",
        "allow_synthetic_unverified": True,
    }
    source.update(source_overrides or {})
    overrides: dict = {"catalogs": [source], "principals": principals}
    if refresh_seconds is not None:
        overrides["api"] = {"catalog_refresh_seconds": refresh_seconds}
    return overrides


def _write_catalog(path, models) -> None:
    path.write_text(json.dumps({"models": models}), encoding="utf-8")


def _chat(client, model: str, *, task_id: str = "task-dyn-1", **extra):
    metadata = {"task_id": task_id, "workspace_id": "ws-alpha"}
    metadata.update(extra.pop("metadata", {}) or {})
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "say hi"}],
        "metadata": metadata,
    }
    body.update(extra)
    return client.post("/v1/chat/completions", json=body)


# --------------------------------------------------------------------- read


def test_discovered_model_without_preset_appears_in_catalog(
    system_factory, tmp_path
):
    """A discovered third model with no per-model preset is listed under an
    explicitly permitted catalog source."""
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(
        catalog_file,
        [
            {"model_id": "mock-model", "display_name": "Mock Model"},
            {"model_id": "mock-model-2", "display_name": "Mock Model 2"},
            {"model_id": "mock-model-3", "display_name": "Mock Model 3"},
        ],
    )
    system = system_factory(
        config_overrides=_catalog_config(),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        response = client.get("/api/v1/catalog")
    assert response.status_code == 200
    payload = response.json()
    runner = next(r for r in payload["data"] if r["instance_id"] == "runner-1")
    assert runner["driver_id"] == "mock"
    ids = {m["model_id"] for m in runner["models"]}
    assert {"mock-model", "mock-model-2", "mock-model-3"} <= ids
    # Discovery is not execution authorization: the principal has a read-only
    # grant, so the extra entries are visible but not executable.
    by_id = {m["model_id"]: m for m in runner["models"]}
    assert by_id["mock-model-3"]["executable"] is False


def test_catalog_requires_auth_and_grant(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(catalog_file, [{"model_id": "mock-model"}])
    system = system_factory(
        config_overrides=_catalog_config(),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    # No key at all -> 401.
    import httpx

    with httpx.Client(base_url=system.base_url, timeout=15.0) as anon:
        assert anon.get("/api/v1/catalog").status_code == 401
    with system.client() as client:
        assert client.get("/api/v1/catalog").status_code == 200


def test_ungranted_principal_sees_no_catalog(system_factory, tmp_path):
    """A principal with no catalog grant gets an empty list, not the models."""
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(catalog_file, [{"model_id": "mock-model"}])
    system = system_factory(
        config_overrides=_catalog_config(allowed_catalogs=[]),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        response = client.get("/api/v1/catalog")
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_catalog_endpoint_absent_when_unconfigured(system_factory):
    system = system_factory()
    with system.client() as client:
        assert client.get("/api/v1/catalog").status_code == 404


# ------------------------------------------------------------------ execute


def test_read_only_grant_cannot_execute_dynamic_alias(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(catalog_file, [{"model_id": "mock-model"}, {"model_id": "mock-model-3"}])
    system = system_factory(
        config_overrides=_catalog_config(),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        # Visible in the catalog...
        catalog = client.get("/api/v1/catalog").json()["data"][0]
        by_id = {m["model_id"]: m for m in catalog["models"]}
        assert by_id["mock-model-3"]["executable"] is False
        # ...but not runnable: the read grant never authorizes execution.
        response = _chat(client, "mock/mock-model-3")
        assert response.status_code == 403


def test_executable_grant_runs_dynamic_alias_end_to_end(system_factory, tmp_path):
    """API -> Core resolve -> Runner -> fake CLI: a discovered model with no
    preset executes and the binding evidence is reported."""
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(
        catalog_file,
        [
            {"model_id": "mock-model"},
            {"model_id": "mock-model-3", "display_name": "Mock Model 3"},
        ],
    )
    system = system_factory(
        config_overrides=_catalog_config(executable_catalogs=["mock-catalog"]),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        # The compatible list advertises the executable dynamic alias.
        listed = client.get("/v1/models").json()["data"]
        dyn = next(m for m in listed if m["id"] == "mock/mock-model-3")
        assert dyn["dynamic"] is True and dyn["catalog"] == "mock-catalog"

        response = _chat(client, "mock/mock-model-3")
    assert response.status_code == 200, response.text
    body = response.json()
    binding = body["run"]["model"]
    assert binding["model_id"] == "mock-model-3"
    assert binding["resolved_model"] == "mock-model-3"
    assert binding["dynamic"] is True
    assert body["run"]["status"] == "completed"


def test_dynamic_alias_unknown_model_is_404(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(catalog_file, [{"model_id": "mock-model"}])
    system = system_factory(
        config_overrides=_catalog_config(executable_catalogs=["mock-catalog"]),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        assert _chat(client, "mock/never-existed").status_code == 404
        # An invented id under an unconfigured prefix is equally unknown.
        assert _chat(client, "agy/gemini-9-ultra").status_code == 404


def test_source_model_allowlist_blocks_unlisted(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(
        catalog_file,
        [{"model_id": "mock-model"}, {"model_id": "mock-secret"}],
    )
    system = system_factory(
        config_overrides=_catalog_config(
            executable_catalogs=["mock-catalog"],
            source_overrides={"models": ["mock-model"]},
        ),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        catalog = client.get("/api/v1/catalog").json()["data"][0]
        by_id = {m["model_id"]: m for m in catalog["models"]}
        assert by_id["mock-secret"]["executable"] is False
        assert "approved model list" in (by_id["mock-secret"]["rejection"] or "")
        assert _chat(client, "mock/mock-secret").status_code == 404


def test_source_cost_tier_filter(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(
        catalog_file,
        [
            {"model_id": "mock-model", "cost_tier": "free"},
            {"model_id": "mock-paid", "cost_tier": "paid"},
        ],
    )
    system = system_factory(
        config_overrides=_catalog_config(
            executable_catalogs=["mock-catalog"],
            source_overrides={"cost_tiers": ["free"]},
        ),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        catalog = client.get("/api/v1/catalog").json()["data"][0]
        by_id = {m["model_id"]: m for m in catalog["models"]}
        assert by_id["mock-model"]["executable"] is True
        assert by_id["mock-paid"]["executable"] is False
        assert _chat(client, "mock/mock-paid").status_code == 404


def test_driver_not_executable_entry_visible_but_denied(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(
        catalog_file,
        [
            {"model_id": "mock-model"},
            {"model_id": "mock-disabled", "executable": False},
        ],
    )
    system = system_factory(
        config_overrides=_catalog_config(executable_catalogs=["mock-catalog"]),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        catalog = client.get("/api/v1/catalog").json()["data"][0]
        by_id = {m["model_id"]: m for m in catalog["models"]}
        assert by_id["mock-disabled"]["driver_executable"] is False
        assert by_id["mock-disabled"]["executable"] is False
        assert _chat(client, "mock/mock-disabled").status_code == 404


# ------------------------------------------------------------- refresh / TTL


def test_catalog_add_remove_after_ttl_without_restart(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(catalog_file, [{"model_id": "mock-model"}])
    system = system_factory(
        config_overrides=_catalog_config(
            executable_catalogs=["mock-catalog"], refresh_seconds=0.4
        ),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        ids = lambda: {
            m["model_id"]
            for r in client.get("/api/v1/catalog").json()["data"]
            for m in r["models"]
        }
        assert "mock-model-4" not in ids()

        # Addition: a new fixture model appears after the TTL, no restart.
        _write_catalog(
            catalog_file,
            [{"model_id": "mock-model"}, {"model_id": "mock-model-4"}],
        )
        time.sleep(0.5)
        assert "mock-model-4" in ids()
        assert _chat(client, "mock/mock-model-4", task_id="task-add").status_code == 200

        # Removal: the dropped id stops resolving after the TTL.
        _write_catalog(catalog_file, [{"model_id": "mock-model"}])
        time.sleep(0.5)
        assert "mock-model-4" not in ids()
        assert _chat(client, "mock/mock-model-4", task_id="task-rm").status_code == 404


def test_failed_refresh_preserves_safe_stale(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(catalog_file, [{"model_id": "mock-model"}])
    system = system_factory(
        config_overrides=_catalog_config(
            executable_catalogs=["mock-catalog"], refresh_seconds=0.4
        ),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        assert _chat(client, "mock/mock-model", task_id="task-ok").status_code == 200
        # Corrupt the catalog: a malformed refresh must not authorize anything.
        catalog_file.write_text("{not json", encoding="utf-8")
        time.sleep(0.5)
        response = client.get("/api/v1/catalog")
        entry = response.json()["data"][0]
        assert entry["ok"] is False
        assert _chat(client, "mock/mock-model", task_id="task-stale").status_code in (
            404,
            503,
        )


# ------------------------------------------------------------- effort schema


def _effort_catalog(path) -> None:
    _write_catalog(
        path,
        [
            {"model_id": "mock-model"},
            {
                "model_id": "mock-effort",
                "effort": "selectable",
                "effort_options": ["low", "high"],
            },
            {
                "model_id": "mock-variant",
                "effort": "model_variant",
                "effort_variants": {"high": "mock-effort"},
            },
        ],
    )


def test_effort_reaches_the_driver_end_to_end(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _effort_catalog(catalog_file)
    system = system_factory(
        config_overrides=_catalog_config(executable_catalogs=["mock-catalog"]),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        response = _chat(
            client, "mock/mock-effort", reasoning_effort="high", task_id="task-e1"
        )
    assert response.status_code == 200, response.text
    binding = response.json()["run"]["model"]
    assert binding["reasoning_effort"] == "high"
    assert binding["resolved_model"] == "mock-effort"
    assert binding["effort_support"] == "selectable"


def test_effort_variant_resolves_to_authorized_catalog_id(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _effort_catalog(catalog_file)
    system = system_factory(
        config_overrides=_catalog_config(executable_catalogs=["mock-catalog"]),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        response = _chat(
            client, "mock/mock-variant", reasoning_effort="high", task_id="task-e2"
        )
    assert response.status_code == 200, response.text
    binding = response.json()["run"]["model"]
    assert binding["reasoning_effort"] == "high"
    assert binding["resolved_model"] == "mock-effort"
    assert binding["effort_support"] == "model_variant"


def test_effort_on_unknown_support_is_422(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(catalog_file, [{"model_id": "mock-model"}])
    system = system_factory(
        config_overrides=_catalog_config(executable_catalogs=["mock-catalog"]),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        response = _chat(
            client, "mock/mock-model", reasoning_effort="high", task_id="task-e3"
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_capability"


def test_effort_option_not_advertised_is_422(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _effort_catalog(catalog_file)
    system = system_factory(
        config_overrides=_catalog_config(executable_catalogs=["mock-catalog"]),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        response = _chat(
            client, "mock/mock-effort", reasoning_effort="maximum", task_id="task-e4"
        )
    assert response.status_code == 422


def test_effort_malformed_values_rejected_before_runner(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _effort_catalog(catalog_file)
    system = system_factory(
        config_overrides=_catalog_config(executable_catalogs=["mock-catalog"]),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        for bad in ("--help", "-x", "HIGH", "a/b", "high;rm", "", 3, True, ["high"]):
            response = _chat(
                client,
                "mock/mock-effort",
                reasoning_effort=bad,
                task_id=f"task-bad-{type(bad).__name__}-{len(str(bad))}",
            )
            assert response.status_code == 400, (bad, response.text)


def test_metadata_effort_carrier_agrees_or_conflicts(system_factory, tmp_path):
    """The metadata carrier (which survives gateways) must agree exactly with
    the top-level field when both are present."""
    catalog_file = tmp_path / "catalog.json"
    _effort_catalog(catalog_file)
    system = system_factory(
        config_overrides=_catalog_config(executable_catalogs=["mock-catalog"]),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        # metadata-only carrier works (this is what a stripping gateway sends)
        ok = _chat(
            client,
            "mock/mock-effort",
            metadata={"reasoning_effort": "high"},
            task_id="task-m1",
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["run"]["model"]["reasoning_effort"] == "high"

        # agreeing duplicates are accepted
        ok2 = _chat(
            client,
            "mock/mock-effort",
            reasoning_effort="low",
            metadata={"reasoning_effort": "low"},
            task_id="task-m2",
        )
        assert ok2.status_code == 200, ok2.text

        # disagreement is a 400, before any Runner effect
        conflict = _chat(
            client,
            "mock/mock-effort",
            reasoning_effort="low",
            metadata={"reasoning_effort": "high"},
            task_id="task-m3",
        )
        assert conflict.status_code == 400


def test_other_metadata_fields_still_rejected(system_factory, tmp_path):
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(catalog_file, [{"model_id": "mock-model"}])
    system = system_factory(
        config_overrides=_catalog_config(),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        response = _chat(
            client, "mock/text", metadata={"runtime_hint": "x"}, task_id="task-mm"
        )
        assert response.status_code == 400


def test_different_effort_same_task_is_not_cached(system_factory, tmp_path):
    """reasoning_effort is part of the request hash: a different effort under
    the same task id must not silently return the cached earlier run."""
    catalog_file = tmp_path / "catalog.json"
    _effort_catalog(catalog_file)
    system = system_factory(
        config_overrides=_catalog_config(executable_catalogs=["mock-catalog"]),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        first = _chat(
            client, "mock/mock-effort", reasoning_effort="low", task_id="task-hash"
        )
        assert first.status_code == 200
        assert first.headers.get("X-Run-Cached") != "true"
        second = _chat(
            client, "mock/mock-effort", reasoning_effort="high", task_id="task-hash"
        )
        # A changed logical request conflicts rather than replays the cache.
        assert second.status_code in (200, 409)
        if second.status_code == 200:
            assert second.headers.get("X-Run-Cached") != "true"
            assert second.json()["run"]["model"]["reasoning_effort"] == "high"
        # Identical replay is still served from cache.
        third = _chat(
            client, "mock/mock-effort", reasoning_effort="low", task_id="task-hash"
        )
        assert third.status_code == 200
        assert third.headers.get("X-Run-Cached") == "true"


# ------------------------------------------------- static variant authority


def test_static_effort_variant_cannot_bypass_the_model_grant(
    system_factory, tmp_path
):
    """P1 regression: a static preset's model_variant target that this
    principal never authorized must not execute merely because the driver
    marks the row executable — catalog membership and driver_executable are
    admission hints, not the principal's model grant.

    Mirrors the parent probe: no catalog source is configured at all; the
    principal holds only allowed_presets=[mock/text]; the driver reports
    mock-model with variant high -> mock-ungranted (a catalog-only model the
    operator never granted).
    """
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(
        catalog_file,
        [
            {
                "model_id": "mock-model",
                "effort": "model_variant",
                "effort_variants": {"high": "mock-ungranted"},
                "cost_tier": "Free",
            },
            {
                "model_id": "mock-ungranted",
                "cost_tier": "High cost",
                "executable": True,
            },
        ],
    )
    system = system_factory(
        "success",
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        control = _chat(client, "mock/text", task_id="p1-control")
        assert control.status_code == 200, control.text[:400]
        assert control.json()["run"]["status"] == "completed"

        denied = _chat(
            client,
            "mock/text",
            reasoning_effort="high",
            task_id="p1-variant",
        )
        # Refused before model reservation: no run, no attempt, no driver
        # contact for the ungranted target.
        assert denied.status_code == 422, denied.text[:400]
        assert denied.json()["error"]["code"] == "unsupported_capability"
        assert "run" not in denied.json()


def test_static_effort_variant_runs_when_target_is_granted(
    system_factory, tmp_path
):
    """Positive control: the same variant is admitted when the principal
    holds an enabled preset binding the target id on the same runner with a
    compatible task policy."""
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(
        catalog_file,
        [
            {
                "model_id": "mock-model",
                "effort": "model_variant",
                "effort_variants": {"high": "mock-hi"},
                "cost_tier": "Free",
            },
            {"model_id": "mock-hi", "cost_tier": "Free", "executable": True},
        ],
    )
    overrides = {
        "presets": [
            {
                "alias": "mock/text",
                "runner_ref": "runner-1",
                "model_id": "mock-model",
                "allow_synthetic_unverified": True,
            },
            {
                "alias": "mock/high",
                "runner_ref": "runner-1",
                "model_id": "mock-hi",
                "task_policy": "text",
                "allow_synthetic_unverified": True,
            },
        ],
        "principals": [
            {
                "name": "alpha",
                "key_hash": hash_api_key("local-alpha-key"),
                "allowed_presets": ["mock/text", "mock/high"],
                "allowed_workspaces": ["ws-alpha"],
                "max_concurrency": 2,
            }
        ],
    }
    system = system_factory(
        "success",
        config_overrides=overrides,
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        response = _chat(
            client,
            "mock/text",
            reasoning_effort="high",
            task_id="p1-positive",
        )
        assert response.status_code == 200, response.text[:400]
        run = response.json()["run"]
        assert run["status"] == "completed"
        assert run["model"]["resolved_model"] == "mock-hi"
        assert run["model"]["reasoning_effort"] == "high"


# ------------------------------------------------- refresh-before-auth (r1)


def test_catalog_refresh_never_runs_for_unauthenticated_or_ungranted(
    system_factory, tmp_path
):
    """Discovery RPCs are a side effect: only an authenticated principal with
    a readable catalog source may trigger a refresh. An invalid key or an
    authenticated principal with no catalog grant must cause zero runner
    discovery work — proven by breaking the catalog file past the TTL and
    observing the runner health stay ok until an authorized read refreshes
    it."""
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(catalog_file, [{"model_id": "mock-model"}])
    overrides = _catalog_config(refresh_seconds=0.3)
    overrides["principals"].append(
        {
            "name": "beta",
            "key_hash": hash_api_key("local-beta-key"),
            "allowed_presets": ["mock/text"],
            "allowed_workspaces": ["ws-alpha"],
            "max_concurrency": 2,
        }
    )
    system = system_factory(
        "success",
        config_overrides=overrides,
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as alpha, system.client("local-beta-key") as beta:
        warm = alpha.get("/api/v1/catalog")
        assert warm.status_code == 200
        assert warm.json()["data"][0]["ok"] is True

        # Corrupt the catalog *after* the TTL: the next refresh must mark the
        # runner failed. If an unauthorized request triggers it, the failure
        # is observable through health/ready before any authorized read.
        catalog_file.write_text("{ not json", encoding="utf-8")
        time.sleep(0.5)

        unauth = system.client("invalid-key").get("/api/v1/catalog")
        assert unauth.status_code == 401
        granted = beta.get("/api/v1/catalog")
        assert granted.status_code == 200 and granted.json()["data"] == []

        scoped_unknown = alpha.get("/providers/nope/api/v1/catalog")
        assert scoped_unknown.status_code == 404

        # Zero side effects so far: the last verified snapshot is still ok.
        ready = alpha.get("/health/ready")
        assert ready.status_code == 200, ready.text[:300]
        assert ready.json()["detail"]["runners"]["runner-1"]["ok"] is True

        # The authorized reader's own read does trigger the bounded refresh —
        # the malformed catalog now fails closed, visibly.
        fresh = alpha.get("/api/v1/catalog")
        assert fresh.status_code == 200
        assert fresh.json()["data"][0]["ok"] is False


# ------------------------------------------------- r2: scoped refresh + shadow


def _two_source_system(
    system_factory,
    tmp_path,
    *,
    alpha_catalogs=("src-a",),
    alpha_exec=(),
    extra_principals=(),
    refresh_seconds=0.4,
):
    """Two runners, two catalog sources, per-runner RPC-count logs."""
    catalog_a = tmp_path / "catalog-a.json"
    catalog_b = tmp_path / "catalog-b.json"
    rpc_a = tmp_path / "rpc-a.log"
    rpc_b = tmp_path / "rpc-b.log"
    _write_catalog(catalog_a, [{"model_id": "mock-model"}, {"model_id": "a-only"}])
    _write_catalog(catalog_b, [{"model_id": "mock-model"}, {"model_id": "b-only"}])
    principals = [
        {
            "name": "alpha",
            "key_hash": hash_api_key("local-alpha-key"),
            "allowed_presets": ["mock/text"],
            "allowed_workspaces": ["ws-alpha"],
            "allowed_catalogs": list(alpha_catalogs),
            "executable_catalogs": list(alpha_exec),
            "max_concurrency": 2,
        },
        *extra_principals,
    ]
    overrides = {
        "api": {"catalog_refresh_seconds": refresh_seconds},
        "catalogs": [
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
        ],
        "principals": principals,
    }
    system = system_factory(
        runner2=True,
        config_overrides=overrides,
        runner_env={
            "CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_a),
            "CLI_DRIVER_MOCK_RPC_LOG": str(rpc_a),
        },
        runner2_env={
            "CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_b),
            "CLI_DRIVER_MOCK_RPC_LOG": str(rpc_b),
        },
    )
    return system, rpc_a, rpc_b


def _rpc_count(path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def test_catalog_read_refreshes_only_the_readable_runner(
    system_factory, tmp_path
):
    """A principal granted read on source A alone must not trigger discovery
    RPCs on source B's runner — even after the TTL expires."""
    system, rpc_a, rpc_b = _two_source_system(
        system_factory, tmp_path
    )
    base_a, base_b = _rpc_count(rpc_a), _rpc_count(rpc_b)
    time.sleep(0.6)  # expire the refresh window
    with system.client() as client:
        assert client.get("/api/v1/catalog").status_code == 200
    assert _rpc_count(rpc_a) > base_a
    assert _rpc_count(rpc_b) == base_b


def test_catalog_read_both_grants_refreshes_both(system_factory, tmp_path):
    """Positive control: a principal reading both sources refreshes both
    runners once each."""
    system, rpc_a, rpc_b = _two_source_system(
        system_factory, tmp_path, alpha_catalogs=("src-a", "src-b")
    )
    base_a, base_b = _rpc_count(rpc_a), _rpc_count(rpc_b)
    time.sleep(0.6)
    with system.client() as client:
        assert client.get("/api/v1/catalog").status_code == 200
    assert _rpc_count(rpc_a) > base_a
    assert _rpc_count(rpc_b) > base_b


def test_no_readable_catalog_key_causes_zero_rpcs(system_factory, tmp_path):
    """An authenticated key with no readable in-scope catalog performs zero
    discovery RPCs on either runner."""
    beta = {
        "name": "beta",
        "key_hash": hash_api_key("local-beta-key"),
        "allowed_presets": ["mock/review"],
        "allowed_workspaces": ["ws-beta"],
        "max_concurrency": 2,
    }
    system, rpc_a, rpc_b = _two_source_system(
        system_factory, tmp_path, extra_principals=[beta]
    )
    base_a, base_b = _rpc_count(rpc_a), _rpc_count(rpc_b)
    time.sleep(0.6)
    with system.client(key="local-beta-key") as client:
        assert client.get("/api/v1/catalog").status_code == 200
        assert client.get("/api/v1/catalog").json()["data"] == []
    assert _rpc_count(rpc_a) == base_a
    assert _rpc_count(rpc_b) == base_b


def test_static_chat_never_touches_unrelated_catalog_runner(
    system_factory, tmp_path
):
    """A static-preset chat refreshes only its own runner's metadata —
    catalog source B's runner sees zero RPCs even with an expired TTL."""
    system, rpc_a, rpc_b = _two_source_system(
        system_factory, tmp_path, alpha_exec=("src-a",)
    )
    base_a, base_b = _rpc_count(rpc_a), _rpc_count(rpc_b)
    time.sleep(0.6)
    with system.client() as client:
        response = _chat(client, "mock/text", task_id="task-scoped-static")
    assert response.status_code == 200, response.text
    assert _rpc_count(rpc_a) > base_a
    assert _rpc_count(rpc_b) == base_b


def test_denied_and_unknown_aliases_cause_zero_rpcs(system_factory, tmp_path):
    """Denied dynamic aliases and unknown aliases must fail before any
    refresh — zero RPCs on either runner."""
    system, rpc_a, rpc_b = _two_source_system(
        system_factory, tmp_path, alpha_exec=("src-a",)
    )
    base_a, base_b = _rpc_count(rpc_a), _rpc_count(rpc_b)
    time.sleep(0.6)
    with system.client() as client:
        # aux/b-only exists on runner-2 but alpha holds no grant for it.
        assert _chat(client, "aux/b-only", task_id="task-denied").status_code == 403
        # An alias under no configured prefix is simply unknown.
        assert _chat(client, "zzz/none", task_id="task-unknown").status_code == 404
    assert _rpc_count(rpc_a) == base_a
    assert _rpc_count(rpc_b) == base_b


def test_models_endpoint_scopes_refresh_to_relevant_runners(
    system_factory, tmp_path
):
    """GET /v1/models refreshes the granted static preset's runner and
    exec-granted sources — a read-only source's runner stays untouched."""
    beta = {
        "name": "beta",
        "key_hash": hash_api_key("local-beta-key"),
        "allowed_presets": ["mock/review"],
        "allowed_workspaces": ["ws-beta"],
        "allowed_catalogs": ["src-b"],  # read-only grant: no /v1/models rows
        "max_concurrency": 2,
    }
    system, rpc_a, rpc_b = _two_source_system(
        system_factory,
        tmp_path,
        alpha_exec=("src-a",),
        extra_principals=[beta],
    )
    base_a, base_b = _rpc_count(rpc_a), _rpc_count(rpc_b)
    time.sleep(0.6)
    with system.client(key="local-beta-key") as client:
        response = client.get("/v1/models")
    assert response.status_code == 200
    # beta holds only a static preset on runner-1 + a READ grant on src-b:
    # runner-2 must not be refreshed for this listing.
    assert _rpc_count(rpc_a) > base_a
    assert _rpc_count(rpc_b) == base_b


def test_catalog_shadowed_row_is_not_executable(system_factory, tmp_path):
    """A catalog row whose alias equals a static preset is advertised but
    never executable: the static binding owns the alias."""
    catalog_file = tmp_path / "catalog.json"
    _write_catalog(
        catalog_file,
        [{"model_id": "mock-model"}, {"model_id": "text"}],
    )
    system = system_factory(
        config_overrides=_catalog_config(executable_catalogs=["mock-catalog"]),
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        catalog = client.get("/api/v1/catalog").json()["data"][0]
        by_id = {m["model_id"]: m for m in catalog["models"]}
        shadowed = by_id["text"]
        assert shadowed["alias"] == "mock/text"
        assert shadowed["executable"] is False
        assert "shadowed" in shadowed["rejection"]
        # The static binding still executes under its own grant.
        response = _chat(client, "mock/text", task_id="task-shadow")
    assert response.status_code == 200, response.text
    assert response.json()["run"]["model"]["dynamic"] is False



def test_shadowed_exact_grant_cannot_authorize_variant(system_factory, tmp_path):
    """A static-owned exact allowed_presets string must not act as a dynamic
    grant for the catalog row of the same name — for BOTH a static base and a
    dynamic base variant hop (r3 HIGH)."""
    catalog_file = tmp_path / "shadow-catalog.json"
    _write_catalog(
        catalog_file,
        [
            {
                "model_id": "mock-model",
                "display_name": "Mock Model",
                "effort": "model_variant",
                "effort_variants": {"high": "ungranted"},
            },
            {
                "model_id": "ungranted",
                "display_name": "Ungranted",
            },
        ],
    )
    principals = [
        {
            "name": "alpha",
            "key_hash": hash_api_key("local-alpha-key"),
            # 'mock/ungranted' is a STATIC preset below — the same string must
            # not be reinterpreted as a dynamic-catalog grant.
            "allowed_presets": ["mock/text", "mock/ungranted", "mock/mock-model"],
            "allowed_workspaces": ["ws-alpha"],
            "allowed_catalogs": ["mock-catalog"],
            "executable_catalogs": [],
            "max_concurrency": 2,
        }
    ]
    presets = [
        {
            "alias": "mock/text",
            "runner_ref": "runner-1",
            "model_id": "mock-model",
            "allow_synthetic_unverified": True,
        },
        {
            # Static preset owns the 'mock/ungranted' alias but binds a
            # DIFFERENT physical model — the grant means this preset, not
            # the catalog's 'ungranted' row.
            "alias": "mock/ungranted",
            "runner_ref": "runner-1",
            "model_id": "mock-model",
            "allow_synthetic_unverified": True,
        },
    ]
    system = system_factory(
        config_overrides={
            "catalogs": [
                {
                    "name": "mock-catalog",
                    "runner_ref": "runner-1",
                    "alias_prefix": "mock/",
                    "allow_synthetic_unverified": True,
                }
            ],
            "principals": principals,
            "presets": presets,
        },
        runner_env={"CLI_DRIVER_MOCK_CATALOG_FILE": str(catalog_file)},
    )
    with system.client() as client:
        # Positive control: the static preset still authorizes and runs its
        # own physical model.
        resp = _chat(client, "mock/ungranted", task_id="task-sh-1")
        assert resp.status_code == 200, resp.text
        assert resp.json()["run"]["model"]["resolved_model"] == "mock-model"
        # Static-base variant hop must NOT be admitted by the shadowed grant.
        resp = _chat(
            client, "mock/text", reasoning_effort="high", task_id="task-sh-2"
        )
        assert resp.status_code == 422, resp.text
        # Dynamic-base variant hop (mock/mock-model is a legitimate
        # non-shadowed exact grant) must NOT be admitted either.
        resp = _chat(client, "mock/mock-model", task_id="task-sh-3")
        assert resp.status_code == 200, resp.text
        resp = _chat(
            client,
            "mock/mock-model",
            reasoning_effort="high",
            task_id="task-sh-4",
        )
        assert resp.status_code == 422, resp.text
