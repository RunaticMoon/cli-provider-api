"""Policy compiler — renders ONE central policy into 9Router payloads.

The compiler is the only place an ordered candidate list becomes concrete
configuration: provider-node / provider / combo payloads for 9Router plus an
advisory wrapper-preset fragment. Jev never sees this; the dispatcher never
carries a fallback list — the compiled combo owns the order.

``compile_plan`` is a pure function: dry-run output is secret-free by
construction. ``apply_plan`` is an explicit opt-in that only targets a
declared ``disposable`` loopback gateway target, reads the credential from a
file (never argv), and reads back the created combos to verify exact order.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from .models import Tier
from .policy import Policy, load_policy

# Tiers whose combos are allowed to exist but stay non-operational until the
# shared core no-post-dispatch-retry guard is verified integrated.
_MUTATION_TIERS = {Tier.STANDARD, Tier.HARD, Tier.MAX}


class CompileError(Exception):
    """Plan/apply failure."""


def _combo_name(route: str) -> str:
    return f"jev.{route}"


def compile_plan(policy: Policy) -> dict:
    """Pure rendering — no secrets, no network. Consumed by ``--dry-run`` and
    by ``apply_plan``."""
    prefix = policy.gateway.node_prefix
    wrapper_base = (policy.gateway.wrapper_base_url or "").rstrip("/")
    by_id = policy.backend_map()

    provider_nodes = []
    if wrapper_base:
        provider_nodes.append({
            "name": prefix,
            "type": "openai-compatible",
            "apiType": "chat",
            "prefix": prefix,
            "baseUrl": f"{wrapper_base}/v1",
        })
    providers = []
    if provider_nodes:
        providers.append({
            "name": prefix,
            "provider": "<node id resolved at apply>",
            "apiKey": "<credential file, never argv>",
        })

    combos = []
    presets = []
    for route_name, route in sorted(policy.routes.items()):
        tier = route_name.split(".")[-1]
        members = []
        all_live = True
        for candidate_id in route.candidates:
            backend = by_id[candidate_id]
            members.append(f"{prefix}/{backend.preset or backend.model}")
            if not backend.enabled or backend.requires_canary:
                all_live = False
        operational = all_live and (
            tier not in {t.value for t in _MUTATION_TIERS}
            or policy.gateway.assume_core_guard
        )
        combos.append({
            "name": _combo_name(route_name),
            "route": route_name,
            "models": members,
            "operational": operational,
            "note": (
                "order is the compiled fallback chain; "
                + ("operational" if operational else
                   "non-operational: disabled/canary member or core guard "
                   "unattested")
            ),
        })
    for backend in policy.backends:
        presets.append({
            "alias": backend.preset or backend.model,
            "driver": backend.driver,
            "backend_id": backend.id,
            "transport": backend.transport,
            "model": backend.model,
            "effort_map": backend.effort_map,
            "enabled": backend.enabled,
            "requires_canary": backend.requires_canary,
        })
    return {
        "schema_version": 1,
        "policy_version": policy.policy_version,
        "provider_nodes": provider_nodes,
        "providers": providers,
        "combos": combos,
        "presets": presets,
        "settings": {
            "comboStrategy": "fallback",
            "fallbackStrategy": "fill-first",
        },
    }


def _target_for(policy: Policy, name: str):
    target = next((t for t in policy.gateway.targets if t.name == name), None)
    if target is None:
        raise CompileError(f"no gateway target {name!r} in policy")
    if target.kind != "disposable":
        raise CompileError(
            f"gateway target {name!r} is not disposable — this slice refuses "
            "production targets"
        )
    url = target.url.rstrip("/")
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise CompileError(
            f"gateway target {name!r} is not loopback ({url}) — refusing"
        )
    return target


def _http(url: str, method: str, body: dict | None, token: str | None,
          timeout: float) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"raw": raw[:400]}
        raise CompileError(
            f"{method} {url} -> HTTP {exc.code}: {parsed}"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CompileError(f"{method} {url} transport failure: {exc}") from exc


def apply_plan(
    policy: Policy,
    *,
    target_name: str,
    credential_file: str,
    timeout_seconds: float = 30.0,
) -> dict:
    """Apply the compiled plan to a disposable loopback 9Router target.

    The management credential is read from ``credential_file`` at call time.
    After applying, combos are read back and compared exactly — a drifted
    order is a hard failure, not a warning.
    """
    target = _target_for(policy, target_name)
    # Credential file is JSON: {"management_token": ..., "upstream_key": ...}.
    # management_token authenticates to 9Router's /api/*; upstream_key is the
    # wrapper bearer the provider entry forwards. Never argv, never logged.
    try:
        creds = json.loads(Path(credential_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CompileError(f"cannot read credential file: {exc}") from exc
    token = creds.get("management_token")
    upstream_key = creds.get("upstream_key")
    if not token or not upstream_key:
        raise CompileError(
            "credential file must hold 'management_token' and 'upstream_key'"
        )

    plan = compile_plan(policy)
    base = target.url.rstrip("/")
    created = {"provider_nodes": [], "providers": [], "combos": [],
               "settings": None, "readback": None}

    status, _ = _http(f"{base}/api/settings", "PATCH", plan["settings"],
                      token, timeout_seconds)
    created["settings"] = {"status": status}

    node_ids = {}
    for node in plan["provider_nodes"]:
        status, body = _http(f"{base}/api/provider-nodes", "POST", node,
                             token, timeout_seconds)
        node_id = (body.get("node") or {}).get("id")
        node_ids[node["name"]] = node_id
        created["provider_nodes"].append({"name": node["name"], "id": node_id,
                                          "status": status})
    for provider in plan["providers"]:
        payload = {
            "name": provider["name"],
            "provider": node_ids[provider["name"]],
            "apiKey": upstream_key,
        }
        status, _ = _http(f"{base}/api/providers", "POST", payload,
                          token, timeout_seconds)
        created["providers"].append({"name": provider["name"],
                                     "status": status})
    for combo in plan["combos"]:
        status, _ = _http(
            f"{base}/api/combos", "POST",
            {"name": combo["name"], "models": combo["models"]},
            token, timeout_seconds,
        )
        created["combos"].append({"name": combo["name"], "status": status})

    # Readback: exact name + ordered models, or the apply failed.
    _, body = _http(f"{base}/api/combos", "GET", None, token, timeout_seconds)
    values = body if isinstance(body, list) else body.get("combos", [])
    by_name = {c.get("name"): c for c in values}
    mismatched = []
    for combo in plan["combos"]:
        got = by_name.get(combo["name"])
        if got is None or got.get("models") != combo["models"]:
            mismatched.append(combo["name"])
    if mismatched:
        raise CompileError(
            f"combo readback mismatch for {mismatched} — applied order does "
            "not match compiled policy"
        )
    created["readback"] = "verified"
    return created


def compile_from_path(policy_path: str | Path) -> dict:
    return compile_plan(load_policy(policy_path))
