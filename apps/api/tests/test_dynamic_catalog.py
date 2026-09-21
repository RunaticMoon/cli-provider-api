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
