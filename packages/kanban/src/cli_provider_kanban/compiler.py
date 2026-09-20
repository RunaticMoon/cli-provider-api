"""Policy compiler — renders ONE central policy into 9Router payloads.

The compiler is the only place an ordered candidate list becomes concrete
configuration: provider-node / provider / combo payloads for 9Router plus a
wrapper-preset fragment. Jev never sees this; the dispatcher never
carries a fallback list — the compiled combo owns the order.

Fail closed by construction:

* Combo ``models`` carry ONLY eligible members — enabled, canary-verified,
  capability-mapped, and inside the compiler-verified driver contract —
  preserving policy order. A route with no eligible members is *held*
  (visible in the plan, never applied). An enabled+routed member that
  violates the verified driver contract is an actionable CompileError,
  never a silently-registered model.
* Every compiled route is effectful (native-agent / API tool-loop traffic
  can mutate at ANY tier), so the shared core no-post-dispatch-retry guard
  attestation (``gateway.assume_core_guard``) is required for every combo —
  not a difficulty-tier subset. ``apply_plan`` refuses to write any
  non-operational combo BEFORE the first HTTP mutating call.
* ``compile_plan`` is pure: dry-run output is secret-free by construction.
* ``apply_plan`` is an explicit opt-in that only targets a declared
  ``disposable`` loopback target (parsed via ``urllib.parse``; the
  installed service port 20128 is refused even when marked disposable),
  authenticates over the REAL management contract — ``/api/auth/login`` ->
  ``auth_token`` cookie session (or a supplied session cookie), never an
  invented management Bearer — reads credentials from an operator-private
  file (never argv, never logged), and reads back node/provider/combo/
  settings to verify exact order before reporting success.
* ``apply_plan`` is CREATE-ONLY onto a FRESH disposable gateway — never a
  reconciler. After the management session is established and BEFORE the
  first configuration write it GETs ``/api/combos``, ``/api/provider-nodes``
  and ``/api/providers``; if ANY catalog is non-empty — even an unrelated
  namespace, because ``PATCH /api/settings`` retunes every combo on the
  target — it refuses with a fixed CompileError, leaves the existing state
  untouched, and tells the operator to point the target at a NEW isolated
  disposable instance. A stale ``jev.*`` route is therefore never reported
  applied while still serving old members, and a re-apply can never
  duplicate nodes/connections before hitting the combo-name collision.
  Preflight is not remote atomicity: the contract still requires exclusive
  operator ownership of the disposable target, and a race or mid-apply
  network failure can leave partial state — the operator preserves,
  discards and rebuilds the target; there is no automatic replay.
* Management error bodies are never echoed: a provider response can carry
  the upstream key itself. Errors carry method + path + HTTP status only.

Effort is intent, never a wire parameter: this MVP applies only the static
native-agent ``auto`` hint; non-auto hints are refused upstream by the
dispatch owner (``resolve_effort``/``EffortUnsupported``). 9Router has no
effort parameter and none is emitted — the policy's verified ``effort_map``
is preserved in the plan under ``effort.intent`` only.
"""

from __future__ import annotations

import http.cookies
import json
import os
import stat
from pathlib import Path
from typing import Mapping

from cli_provider_sdk import validate_alias

from .models import Tier  # noqa: F401  (re-exported for callers)
from .policy import Policy, load_policy
from .wrapper_client import (
    WrapperError,
    _bounded_request,
    _LoopbackBase,
    _TransportFailure,
)

_MAX_CREDENTIAL_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class CompileError(Exception):
    """Plan/apply failure."""


# ---------------------------------------------------------------------------
# Compiler-verified driver contract.
#
# Mirrored from the actual driver sources — an enabled+routed backend outside
# this table is a CompileError, never a registered combo member:
#
# * ``hermes-api`` (drivers/hermes-api): run requests must carry one of the
#   two pinned ``ProviderPreset`` aliases — ``bai/deepseek-v4.1-flash`` or
#   ``commandcode/deepseek-v4.1-flash``; anything else fails preflight with
#   ``unknown_preset``. ``discover_models`` reports ``provider:model-tail``
#   descriptor ids — those are the ``PresetConfig.model_id`` values the core
#   registry verifies against.
# * ``devin`` (drivers/devin): the preset alias is operator-chosen (the
#   driver never compares it); the pinned supported model is exactly
#   ``swe-2-max`` and its descriptor id is ``swe-2-max``.
#
# The checked-in example policy still uses the invented
# ``hermes-api/bai-deepseek-v4.1-flash`` / ``hermes-api/cc-deepseek-v4.1-flash``
# aliases — the driver rejects them; the required example edits are reported
# to the parent in docs/JEV_ROUTING_CONTRACT.md.
# ---------------------------------------------------------------------------

DRIVER_CONTRACT = {
    "hermes-api": {
        "kinds": frozenset({"bai", "commandcode"}),
        # kind -> pinned alias -> (wire model id, registry descriptor id)
        "preset_models": {
            "bai/deepseek-v4.1-flash": (
                "deepseek-v4.1-flash", "bai:deepseek-v4.1-flash"),
            "commandcode/deepseek-v4.1-flash": (
                "deepseek/deepseek-v4.1-flash",
                "commandcode:deepseek-v4.1-flash"),
        },
        "kind_preset": {
            "bai": "bai/deepseek-v4.1-flash",
            "commandcode": "commandcode/deepseek-v4.1-flash",
        },
    },
    "devin": {
        "kinds": frozenset({"devin"}),
        "models": frozenset({"swe-2-max"}),
    },
}


def _combo_name(route: str) -> str:
    return f"jev.{route}"


def _member_eligibility(backend, capability: str) -> str | None:
    """None when the backend may be registered as an active combo member."""
    if not backend.enabled:
        reason = "backend disabled"
        if backend.disabled_reason:
            reason += f" ({backend.id}: {backend.disabled_reason})"
        return reason
    if backend.requires_canary:
        return "canary verification pending — declared, not yet proven"
    if backend.capabilities.get(capability) is not True:
        return f"capability {capability!r} not mapped on backend"
    return None


def _member_model(backend) -> str:
    """Validate an *eligible* routed backend against the known driver
    contract; return the upstream model string for the combo member."""
    driver = backend.driver
    contract = DRIVER_CONTRACT.get(driver)
    if contract is None:
        raise CompileError(
            f"backend {backend.id!r} is routed+enabled but driver "
            f"{driver!r} has no compiler-verified contract — refusing to "
            "register an unverifiable model"
        )
    if backend.kind not in contract["kinds"]:
        raise CompileError(
            f"backend {backend.id!r}: kind {backend.kind!r} is not served "
            f"by driver {driver!r}"
        )
    member_model = backend.preset or backend.model
    if not member_model:
        raise CompileError(
            f"backend {backend.id!r} carries no preset/model to register")
    try:
        validate_alias(member_model)
    except ValueError as exc:
        raise CompileError(
            f"backend {backend.id!r} preset {member_model!r} is not a "
            f"valid alias: {exc}"
        ) from exc
    preset_models = contract.get("preset_models")
    if preset_models is not None:
        expected = contract["kind_preset"].get(backend.kind)
        if expected is not None and member_model != expected:
            raise CompileError(
                f"backend {backend.id!r} (kind {backend.kind!r}) must use "
                f"the driver-pinned preset {expected!r}, got "
                f"{member_model!r} — the driver would reject it with "
                "unknown_preset"
            )
        pinned = preset_models.get(member_model)
        if pinned is None:
            raise CompileError(
                f"backend {backend.id!r} preset {member_model!r} is not a "
                f"pinned alias of driver {driver!r} — the driver would "
                f"reject it with unknown_preset; expected one of "
                f"{sorted(preset_models)}"
            )
        wire_model, _descriptor = pinned
        if backend.model != wire_model:
            raise CompileError(
                f"backend {backend.id!r} model {backend.model!r} does not "
                f"match the driver-pinned model {wire_model!r} for preset "
                f"{member_model!r}"
            )
    else:
        supported = contract["models"]
        if backend.model not in supported:
            raise CompileError(
                f"backend {backend.id!r} model {backend.model!r} is not "
                f"supported by driver {driver!r} — supported models: "
                f"{sorted(supported)}"
            )
    return member_model


def _descriptor_for(backend) -> str | None:
    """Registry model id (driver discover_models descriptor) when the
    contract can prove it, else None."""
    contract = DRIVER_CONTRACT.get(backend.driver)
    if contract is None:
        return None
    preset_models = contract.get("preset_models")
    if preset_models is not None:
        pinned = preset_models.get(backend.preset or "")
        return pinned[1] if pinned else None
    if backend.model in contract["models"]:
        return backend.model
    return None


def _preset_fragments(policy: Policy, runners: Mapping[str, str]):
    """Render the operator wrapper preset list.

    Entries are REAL ``PresetConfig`` fields only when the contract pins the
    model id AND an operator runner mapping resolves the runner_ref;
    otherwise the entry lands in ``presets_advisory`` marked explicitly NOT
    loadable configuration.
    """
    presets: list[dict] = []
    advisory: list[dict] = []
    for backend in policy.backends:
        alias = backend.preset or backend.model
        if not alias:
            continue
        descriptor = _descriptor_for(backend)
        runner_ref = (
            runners.get(backend.id)
            or runners.get(backend.driver or "")
        )
        if runner_ref and descriptor:
            presets.append({
                "alias": alias,
                "runner_ref": runner_ref,
                "model_id": descriptor,
                "task_policy": "text",
                # A backend still awaiting canary proof must never become an
                # ENABLED loadable OperatorConfig entry — routing drops it
                # and the direct-preset surface stays disabled too.
                "enabled": backend.enabled and not backend.requires_canary,
            })
            continue
        reasons = []
        if not runner_ref:
            reasons.append(
                f"no operator runner mapping for driver {backend.driver!r}")
        if not descriptor:
            reasons.append(
                "model is not in the compiler-verified driver contract")
        advisory.append({
            "alias": alias,
            "model_id": descriptor or backend.model,
            "enabled": backend.enabled,
            "advisory": "; ".join(reasons)
            + " — advisory only, NOT loadable OperatorConfig",
        })
    return presets, advisory


def _effort_block(policy: Policy) -> dict:
    return {
        "applied_hints": ["auto"],
        "intent": {
            b.id: dict(b.effort_map)
            for b in policy.backends
            if b.effort_map
        },
        "note": (
            "MVP: only the static native-agent 'auto' intent is executable; "
            "non-auto hints are refused upstream by the dispatch owner "
            "(EffortUnsupported). 9Router has no effort parameter — none is "
            "emitted. The verified per-backend effort_map is preserved here "
            "as intent only."
        ),
    }


def compile_plan(
    policy: Policy, *, runner_map: Mapping[str, str] | None = None
) -> dict:
    """Pure rendering — no secrets, no network. Consumed by ``--dry-run``
    and by ``apply_plan``.

    ``runner_map`` is the operator's driver-or-backend -> runner instance
    mapping; without it the preset fragment is explicitly advisory.
    """
    prefix = policy.gateway.node_prefix
    wrapper_base = (policy.gateway.wrapper_base_url or "").rstrip("/")
    by_id = policy.backend_map()
    runners = dict(runner_map or {})

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
    for route_name, route in sorted(policy.routes.items()):
        capability = route_name.split(".")[1]
        members: list[str] = []
        dropped: list[dict] = []
        seen: set[str] = set()
        for candidate_id in route.candidates:
            backend = by_id[candidate_id]
            reason = _member_eligibility(backend, capability)
            if reason is not None:
                dropped.append({"backend": backend.id, "reason": reason})
                continue
            member_model = _member_model(backend)
            member = f"{prefix}/{member_model}"
            if member in seen:
                # No duplicated fallback leg inside the combo.
                continue
            seen.add(member)
            members.append(member)
        operational = bool(members) and policy.gateway.assume_core_guard
        held = not members
        if held:
            note = "held: no enabled+canary-verified members — never applied"
        elif not operational:
            note = (
                "non-operational: core no-post-dispatch-retry guard "
                "unattested (gateway.assume_core_guard) — apply refuses "
                "before any HTTP write"
            )
        else:
            note = "order is the compiled fallback chain; operational"
        combos.append({
            "name": _combo_name(route_name),
            "route": route_name,
            "models": members,
            "operational": operational,
            "held": held,
            "dropped": dropped,
            "note": note,
        })

    presets, presets_advisory = _preset_fragments(policy, runners)
    return {
        "schema_version": 1,
        "policy_version": policy.policy_version,
        "provider_nodes": provider_nodes,
        "providers": providers,
        "combos": combos,
        "presets": presets,
        "presets_advisory": presets_advisory,
        "effort": _effort_block(policy),
        "settings": {
            "comboStrategy": "fallback",
            "fallbackStrategy": "fill-first",
        },
    }


def _target_for(policy: Policy, name: str) -> tuple[object, _LoopbackBase]:
    target = next((t for t in policy.gateway.targets if t.name == name), None)
    if target is None:
        raise CompileError(f"no gateway target {name!r} in policy")
    if target.kind != "disposable":
        raise CompileError(
            f"gateway target {name!r} is not disposable — this slice refuses "
            "production targets"
        )
    try:
        base = _LoopbackBase(target.url)
    except WrapperError as exc:
        raise CompileError(f"gateway target {name!r}: {exc}") from exc
    return target, base


def _read_credentials(credential_file: str) -> dict:
    """Operator-private JSON credential file — never argv, never logged.

    Contract: ``upstream_key`` (required — the wrapper bearer the provider
    entry forwards) plus EITHER ``management_password`` (real
    ``/api/auth/login`` -> auth_token cookie session) OR
    ``management_cookie`` (an existing auth_token session value). 9Router
    management auth is the dashboard cookie session — there is no invented
    management Bearer permission.
    """
    path = Path(credential_file)
    try:
        st = path.lstat()
    except OSError as exc:
        raise CompileError(f"cannot read credential file: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise CompileError("credential file must not be a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise CompileError("credential file must be a regular file")
    if st.st_uid != os.getuid():
        raise CompileError(
            "credential file must be owned by the current user")
    if st.st_mode & 0o077:
        raise CompileError(
            "credential file mode must not allow group/world access "
            "(chmod 600)"
        )
    if st.st_size > _MAX_CREDENTIAL_BYTES:
        raise CompileError("credential file is too large")
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CompileError(f"credential file is not JSON: {exc}") from exc
    if not isinstance(creds, dict):
        raise CompileError("credential file must be a JSON object")
    if not creds.get("upstream_key"):
        raise CompileError("credential file must hold 'upstream_key'")
    if not creds.get("management_password") and not creds.get(
            "management_cookie"):
        raise CompileError(
            "credential file must hold 'management_password' (real "
            "/api/auth/login) or 'management_cookie' (existing auth_token "
            "session value)"
        )
    return creds


def _extract_auth_token(set_cookie: str) -> str | None:
    for line in set_cookie.split("\n"):
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(line)
        except http.cookies.CookieError:
            continue
        morsel = jar.get("auth_token")
        if morsel is not None and morsel.value:
            return morsel.value
    return None


def _is_cookie_octet(ch: str) -> bool:
    """RFC 6265 cookie-octet: printable ASCII minus ``\"``, ``,``, ``;``,
    ``\\`` and whitespace — no controls, no non-ASCII bytes."""
    o = ord(ch)
    return (
        o == 0x21
        or 0x23 <= o <= 0x2B
        or 0x2D <= o <= 0x3A
        or 0x3C <= o <= 0x5B
        or 0x5D <= o <= 0x7E
    )


def _session_cookie_value(value: str | None, source: str) -> str:
    """ONE narrow validator for the auth_token session value, applied
    identically to the operator-supplied ``management_cookie`` and to the
    token harvested from the target's own ``Set-Cookie``: ASCII only, no
    control characters, no cookie/header separators. The value itself is
    never echoed — it is a secret."""
    if (
        not isinstance(value, str)
        or not value
        or not all(_is_cookie_octet(ch) for ch in value)
    ):
        raise CompileError(f"{source} is not a valid cookie value")
    return value


def _http_json(
    base: _LoopbackBase,
    method: str,
    path: str,
    *,
    body: dict | None,
    cookie: str | None,
    timeout: float,
) -> tuple[int, dict, dict]:
    """One management call — returns (status, parsed_body, headers).

    Errors carry method + path + HTTP status ONLY: a management error body
    can echo the upstream apiKey and is never printed.
    """
    headers = {"Accept": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        status, resp_headers, raw = _bounded_request(
            base, method, path,
            headers=headers, data=data,
            timeout_seconds=timeout,
            max_response_bytes=_MAX_RESPONSE_BYTES,
        )
    except _TransportFailure as exc:
        raise CompileError(
            f"{method} {path} transport failure: {exc}") from exc
    if status >= 300:
        raise CompileError(f"{method} {path} -> HTTP {status}")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CompileError(
            f"{method} {path} -> unparsable response") from exc
    return status, parsed, resp_headers


def _management_session(
    base: _LoopbackBase, creds: dict, timeout: float
) -> str:
    """Real 9Router management auth: /api/auth/login -> auth_token cookie,
    or an operator-supplied existing session cookie value."""
    cookie = creds.get("management_cookie")
    if cookie:
        return f"auth_token={_session_cookie_value(cookie, 'management_cookie')}"
    status, _body, resp_headers = _http_json(
        base, "POST", "/api/auth/login",
        body={"password": creds["management_password"]},
        cookie=None, timeout=timeout,
    )
    token = _extract_auth_token(resp_headers.get("set-cookie", ""))
    if not token:
        raise CompileError("login response carried no auth_token cookie")
    # The target's own Set-Cookie is validated through the SAME narrow
    # contract before it is re-sent — a malformed token is a typed refusal,
    # never an unchecked header value.
    return f"auth_token={_session_cookie_value(token, 'login auth_token')}"


def _catalog_entries(body, key: str, context: str) -> list:
    """Extract one management catalog list — fail closed: a missing or
    non-list catalog is malformed and is NEVER treated as empty."""
    if isinstance(body, list):
        entries = body
    elif isinstance(body, dict) and isinstance(body.get(key), list):
        entries = body[key]
    else:
        raise CompileError(
            f"{context} returned a malformed catalog — refusing to treat a "
            "missing/unparseable list as empty"
        )
    return entries


def _preflight_fresh(
    base: _LoopbackBase, session: str, timeout: float
) -> None:
    """CREATE-ONLY contract: GET every configuration catalog BEFORE the
    first write and refuse unless the target is a fresh disposable
    gateway — ANY existing entry counts, including unrelated namespaces,
    because PATCH /api/settings would retune them all."""
    occupied = []
    for path, key in (
        ("/api/combos", "combos"),
        ("/api/provider-nodes", "nodes"),
        ("/api/providers", "connections"),
    ):
        _, body, _ = _http_json(
            base, "GET", path, body=None, cookie=session, timeout=timeout)
        entries = _catalog_entries(body, key, f"GET {path}")
        if entries:
            occupied.append(f"{path} ({len(entries)} existing)")
    if occupied:
        raise CompileError(
            "refusing to apply to an occupied gateway — preflight found "
            + ", ".join(occupied)
            + ". apply_plan is create-only onto a FRESH disposable target: "
            "PATCH /api/settings is global and would silently retune "
            "unrelated combos, and a stale jev.* route would otherwise be "
            "reported applied while still serving its old members. No "
            "configuration write was issued and the existing state was "
            "left unchanged — point the policy target at a NEW isolated "
            "disposable 9Router instance."
        )


def apply_plan(
    policy: Policy,
    *,
    target_name: str,
    credential_file: str,
    timeout_seconds: float = 30.0,
    runner_map: Mapping[str, str] | None = None,
) -> dict:
    """Apply the compiled plan to a FRESH disposable loopback 9Router.

    Fail closed: any non-operational combo that carries members aborts the
    apply BEFORE the first HTTP mutating write. After login, a preflight
    GET of every configuration catalog must find the target EMPTY — this
    is a create-only contract, not reconciliation: an occupied target
    (stale jev.* route, duplicated node, or an unrelated namespace the
    global settings PATCH would retune) is refused before any write and
    left unchanged; the operator points at a NEW isolated disposable
    gateway instead. Held routes (no eligible members) are skipped and
    reported. After applying, the ENTIRE combo namespace, the provider
    node, the provider binding (by the created connection's canonical id)
    and settings are read back and compared exactly — drift is a hard
    failure, never a warning, and partial success is never reported.
    Preflight is not remote atomicity: a race or mid-apply network
    failure can leave partial state — preserve/discard/rebuild the
    disposable target under exclusive operator ownership; nothing here
    replays automatically.
    """
    _target, base = _target_for(policy, target_name)
    creds = _read_credentials(credential_file)
    upstream_key = creds["upstream_key"]

    plan = compile_plan(policy, runner_map=runner_map)
    blocked = [
        c["name"] for c in plan["combos"]
        if c["models"] and not c["operational"]
    ]
    if blocked:
        raise CompileError(
            f"refusing to apply non-operational combos {blocked} — the "
            "shared core no-post-dispatch-retry guard is not attested "
            "(gateway.assume_core_guard); no HTTP write was issued"
        )

    session = _management_session(base, creds, timeout_seconds)
    # Authenticated preflight BEFORE the first configuration write —
    # an occupied namespace refuses here, never after mutations.
    _preflight_fresh(base, session, timeout_seconds)
    created: dict = {
        "provider_nodes": [], "providers": [], "combos": [],
        "held": [c["name"] for c in plan["combos"] if not c["models"]],
        "settings": None, "readback": None,
        "preflight": "target catalogs empty (create-only fresh target)",
    }

    status, _, _ = _http_json(
        base, "PATCH", "/api/settings",
        body=plan["settings"], cookie=session, timeout=timeout_seconds,
    )
    created["settings"] = {"status": status}

    node_ids: dict[str, str] = {}
    for node in plan["provider_nodes"]:
        status, body, _ = _http_json(
            base, "POST", "/api/provider-nodes",
            body=node, cookie=session, timeout=timeout_seconds,
        )
        node_body = body.get("node") if isinstance(body, dict) else None
        node_id = node_body.get("id") if isinstance(node_body, dict) else None
        if not node_id:
            raise CompileError(
                "provider-node create returned no node id")
        node_ids[node["name"]] = node_id
        created["provider_nodes"].append(
            {"name": node["name"], "id": node_id, "status": status})
    conn_ids: dict[str, str] = {}
    for provider in plan["providers"]:
        payload = {
            "name": provider["name"],
            "provider": node_ids[provider["name"]],
            "apiKey": upstream_key,
        }
        status, body, _ = _http_json(
            base, "POST", "/api/providers",
            body=payload, cookie=session, timeout=timeout_seconds,
        )
        # Capture the created connection's canonical returned id — the
        # readback binds to THAT object, never to the first same-name row.
        connection = body.get("connection") if isinstance(body, dict) else None
        conn_id = connection.get("id") if isinstance(connection, dict) else None
        if not isinstance(conn_id, str) or not conn_id:
            raise CompileError(
                "provider create returned no connection id")
        conn_ids[provider["name"]] = conn_id
        created["providers"].append(
            {"name": provider["name"], "id": conn_id, "status": status})

    applicable = [c for c in plan["combos"] if c["models"]]
    for combo in applicable:
        status, _, _ = _http_json(
            base, "POST", "/api/combos",
            body={"name": combo["name"], "models": combo["models"]},
            cookie=session, timeout=timeout_seconds,
        )
        created["combos"].append({"name": combo["name"], "status": status})

    # Readback — EXACT verification over the WHOLE managed namespace,
    # secrets never printed. On a create-only fresh target the live combo
    # catalog must equal the compiled applicable set exactly: a stale or
    # foreign combo (including a held/omitted route that is still live) is
    # a hard failure — success is never reported from partial members.
    _, combos_body, _ = _http_json(
        base, "GET", "/api/combos", body=None, cookie=session,
        timeout=timeout_seconds)
    combo_entries = _catalog_entries(
        combos_body, "combos", "GET /api/combos")
    live_combos: dict[str, object] = {}
    for entry in combo_entries:
        if not isinstance(entry, dict) or not isinstance(
                entry.get("name"), str):
            raise CompileError(
                "combo readback returned a malformed catalog entry")
        live_combos[entry["name"]] = entry.get("models")
    expected = {c["name"]: c["models"] for c in applicable}
    unexpected = sorted(n for n in live_combos if n not in expected)
    drifted = sorted(
        n for n in expected if live_combos.get(n) != expected[n])
    if unexpected or drifted:
        raise CompileError(
            "combo readback mismatch — the live catalog does not equal the "
            f"compiled set exactly (unexpected present: {unexpected}; "
            f"mismatched or absent: {drifted})"
        )
    if plan["provider_nodes"]:
        _, nodes_body, _ = _http_json(
            base, "GET", "/api/provider-nodes", body=None, cookie=session,
            timeout=timeout_seconds)
        nodes = _catalog_entries(
            nodes_body, "nodes", "GET /api/provider-nodes")
        live_nodes = {
            n["id"]: n for n in nodes
            if isinstance(n, dict) and isinstance(n.get("id"), str)
        }
        foreign = sorted(set(live_nodes) - set(node_ids.values()))
        if foreign or len(nodes) != len(live_nodes):
            raise CompileError(
                "provider-node readback found entries this apply did not "
                "create — the target is not an exclusively-owned fresh "
                "disposable gateway"
            )
        for node in plan["provider_nodes"]:
            match = live_nodes.get(node_ids[node["name"]])
            if (
                match is None
                or match.get("baseUrl") != node["baseUrl"]
                or match.get("prefix") != node["prefix"]
            ):
                raise CompileError(
                    f"provider-node readback mismatch for {node['name']!r}")
    if plan["providers"]:
        _, providers_body, _ = _http_json(
            base, "GET", "/api/providers", body=None, cookie=session,
            timeout=timeout_seconds)
        connections = _catalog_entries(
            providers_body, "connections", "GET /api/providers")
        live_conns = {
            c["id"]: c for c in connections
            if isinstance(c, dict) and isinstance(c.get("id"), str)
        }
        foreign = sorted(set(live_conns) - set(conn_ids.values()))
        if foreign or len(connections) != len(live_conns):
            raise CompileError(
                "provider readback found connections this apply did not "
                "create — the target is not an exclusively-owned fresh "
                "disposable gateway"
            )
        for provider in plan["providers"]:
            # The binding is verified by the canonical id the POST
            # returned plus the created node id; the app never echoes
            # apiKey on GET and it is never printed here.
            match = live_conns.get(conn_ids[provider["name"]])
            if (
                match is None
                or match.get("provider") != node_ids[provider["name"]]
            ):
                raise CompileError(
                    f"provider binding readback mismatch for "
                    f"{provider['name']!r}")
    _, settings_body, _ = _http_json(
        base, "GET", "/api/settings", body=None, cookie=session,
        timeout=timeout_seconds)
    if not isinstance(settings_body, dict):
        raise CompileError(
            "GET /api/settings returned a malformed catalog")
    for key, want in plan["settings"].items():
        if settings_body.get(key) != want:
            raise CompileError(
                f"settings readback mismatch on {key!r}: "
                f"{settings_body.get(key)!r} != {want!r}")

    created["readback"] = "verified"
    return created


def compile_from_path(policy_path: str | Path) -> dict:
    return compile_plan(load_policy(policy_path))
