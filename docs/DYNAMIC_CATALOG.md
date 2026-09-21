# Dynamic CLI catalog + per-model reasoning effort

This slice replaces hand-maintained per-model presets with opt-in native
catalog discovery plus policy-based authorization. Discovery is never
authorization: a driver may enumerate hundreds of real catalog ids while the
operator's source/principal policy decides what is visible and what may run.

## Endpoints

- `GET /api/v1/catalog` — authenticated discovery view. Returns
  `{"object": "list", "data": [<source>, ...]}` where each source reports
  `source`, `instance_id`, `driver_id`, `ok`, `detail`, `cli_version`,
  `observed_at`, `stale`, `refresh_seconds`, and a `models` list. Every model
  entry carries the verbatim driver descriptor (`model_id`, `display_name`,
  `verification`, `effort`, `effort_options`, `effort_variants`, `cost_tier`,
  `family`, `aliases`, `driver_executable`) plus server-decided `alias`,
  `admitted`, `executable`, and `rejection`.
- `GET /providers/{driver_id}/api/v1/catalog` — the same view scoped to one
  driver's manifest `driver_id`.
- `GET /v1/models` — unchanged envelope; now also lists *admitted and
  executable* dynamic aliases (`dynamic: true`, `catalog: <source>`). The
  full not-admitted inventory is intentionally only on the catalog endpoint.
- `POST /v1/chat/completions` — accepts a dynamic alias
  (`<alias_prefix><exact catalog id>`) anywhere a preset alias works, and a
  new `reasoning_effort` field (see below).

## Operator configuration

```yaml
api:
  catalog_refresh_seconds: 60        # bounded TTL; default 60

catalogs:
  - name: agy-main                   # operator-owned source name
    runner_ref: runner-agy-1         # one allowlisted Runner instance
    alias_prefix: "agy/"             # dynamic aliases are agy/<exact id>
    models: ["gemini-3-pro-high"]    # optional exact-id allowlist (omitted = all)
    cost_tiers: ["free"]             # optional cost-tier filter
    task_policy: default             # operator-owned task policy for runs
    allow_synthetic_unverified: false
    enabled: true

principals:
  - name: alpha
    allowed_catalogs: [agy-main]     # READ grant: catalog metadata only
    executable_catalogs: []          # RUN grant: source-wide execution
    allowed_presets: [agy/specific]  # RUN grant: one exact alias (optional)
```

Grant semantics:

- `allowed_catalogs` is a read-only metadata grant. A principal with only it
  sees the source and its models (`executable: false` per entry) and cannot
  run anything through it.
- `executable_catalogs` is a source-wide run grant. Combined with the
  source's own `models`/`cost_tiers` filters it admits future catalog
  entries without new config — that is the point of the feature. A
  conservative deployment omits it and grants individual aliases via
  `allowed_presets` (exact strings; there is no wildcard syntax).
- A dynamic alias additionally requires a verified runner snapshot
  (`health.ok`), a `passed` descriptor verification (or the explicit
  synthetic opt-in), the driver's own `executable` admission, and the
  source's `models`/`cost_tiers` filters. A failed/malformed catalog marks
  the runner not-ok and authorizes nothing — the last good snapshot remains
  visible with `stale: true`/`ok: false`.

Refresh is bounded and singleflight: at most one discovery pass per
`catalog_refresh_seconds` window regardless of request rate, and concurrent
callers share one pass. Additions and removals become visible after the TTL
without a restart; in-flight and durable attempts are never replayed or
invalidated by a removal (idempotency is durable in the Store).

## Per-model reasoning effort

Each descriptor reports a truthful effort mode:

| `effort`          | Meaning                                                        |
|-------------------|----------------------------------------------------------------|
| `unknown`         | no official evidence; effort requests are rejected             |
| `unsupported`     | model is known not to support effort selection                 |
| `selectable`      | `effort_options` lists the exact accepted tokens               |
| `model_variant`   | `effort_variants` maps tokens to exact catalog ids             |

Request shape (`reasoning_effort` is a bounded token, `^[a-z][a-z0-9_]{0,31}$`):

```json
{
  "model": "agy/gemini-3-pro-preview",
  "messages": [{"role": "user", "content": "..."}],
  "reasoning_effort": "low",
  "metadata": {"task_id": "t-1", "workspace_id": "ws-alpha",
               "reasoning_effort": "low"}
}
```

- The top-level field and `metadata.reasoning_effort` are two carriers for
  the same value. Both are strictly type-checked; if both are present they
  must be exactly equal — disagreement is a 400 before any model lookup or
  Runner reservation.
- **`metadata.reasoning_effort` is the gateway-safe carrier.** Measured
  9Router 0.5.81 behaviour: the provider-model route forwards the request
  `metadata` object verbatim but strips top-level `reasoning_effort`.
  Consumers behind 9Router should send effort in `metadata` (duplicating at
  top level is fine — they must agree). The API itself honours either.
- Resolution is by exact descriptor metadata only — never suffix inference.
  `selectable` keeps the same catalog id; `model_variant` resolves to the
  mapped exact id, which is independently re-admitted under the same
  authorization surface (a variant cannot hop to an unapproved model).
- The resolved binding flows through `RunParams.reasoning_effort` +
  `RunParams.resolved_model` to the driver, which re-derives the target from
  its *own* catalog and refuses a mismatch. `run.model` in the response and
  the persisted attempt record carry the requested/resolved ids and effort
  as durable evidence.
- `reasoning_effort` participates in the logical request hash: a replay of
  the same `task_id` with different effort is a content conflict, never a
  silently cached answer. Omitting it keeps the legacy hash, preserving
  cross-provider retry semantics.

### Honest native support matrix

| Driver      | Catalog source                              | Effort evidence today                          |
|-------------|---------------------------------------------|------------------------------------------------|
| mock        | `CLI_DRIVER_MOCK_CATALOG_FILE` JSON fixture | fixture-declared (`selectable`/`model_variant`)|
| antigravity | `agy models` (TSV, pinned version)          | `unknown` — CLI exposes no per-model matrix    |
| devin       | `devin models list --format json`           | `unknown` — ACP offers model configOptions, no effort option (385-model evidence) |

The native drivers parse and advertise every real catalog id with exact
identifiers, but their descriptors honestly report `effort: unknown` until
the upstream CLI documents per-model support. Requesting effort on a native
model is therefore a clean 4xx — never a silently lowered or mis-targeted
run.

## Native driver model policy (execution side)

Discovery enumerating the whole catalog does not mean any model may run:

- **Devin** — `DEVIN_MODELS` is the operator execution allowlist
  (comma-separated exact ids, or `*` to admit any catalog member). It
  defaults to `DEVIN_MODEL` (`swe-2-max`) so existing deployments are
  unchanged. Every executed model must also be present in the fresh official
  catalog and satisfy `DEVIN_EXPECTED_COST_TIER` (default `Free`) unless
  `DEVIN_ALLOWED_COST_TIERS` explicitly admits the model's tier. The spawned
  argv uses `devin --permission-mode <mode> acp --model <exact id>`, the
  request-resolved model is bound per run (no mutable shared driver state),
  and the ACP `session/new` acknowledged model must equal it. Version pin,
  workspace root and permission policy are unchanged.
- **Antigravity** — `AGY_MODELS` remains the operator-level execution
  opt-in (exact ids; empty = deny all; `*` is supported for catalog-trusting
  deployments). Discovery reports the full TSV catalog regardless, and
  descriptors mark `executable` accordingly. `--effort <token>` is emitted
  only for descriptors that declare `selectable` support (fixtures today;
  real `agy` models stay `unknown`). Version pin, init-frame verification,
  and `AGY_ALLOW_SKIP_PERMISSIONS` action gating are unchanged.

## Minimal consumer

`examples/catalog_client.py` is a stdlib-only client: it reads
`/api/v1/catalog`, picks an `executable` entry (optionally requiring an
advertised `selectable` effort option), duplicates the effort into
`metadata` for gateway safety, and submits the chat request. See its
docstring for usage.

## Verification evidence

- `apps/api/tests/test_dynamic_catalog.py` — 20 tests: discovery visibility,
  read-vs-execute grants, dynamic alias execution end-to-end
  (API→UDS Runner→fake CLI), unknown/invented ids, source model/cost
  filters, TTL add/remove without restart, effort carriers
  (top-level/metadata/agree/conflict/type), idempotency-hash separation.
- `packages/core/tests/test_catalog.py` — resolution matrix, variant
  re-admission, singleflight TTL refresh, failed-refresh fails-closed with
  stale reporting, hash differentiation.
- `drivers/devin/tests/test_devin_driver.py` — full-catalog discovery,
  allowlist/tier/cost admission, catalog-selected model execution via the
  fake ACP CLI, zero-effects negatives, session-model ack pinning.
- `drivers/antigravity/tests/test_antigravity_driver.py` — TSV parser
  (exact ids, `(High)/(Medium)/(Low)` labels), discovery decoupled from the
  allowlist, catalog-selected execution with operator opt-in, effort argv
  emission only for declared `selectable` fixtures.
- `tests/integration_9router/test_gateway_safety.py::
  test_gateway_metadata_reasoning_effort_reaches_worker` — real 9Router
  passthrough of the metadata carrier into worker RunParams.

## Known limits

- `agy`/`devin` real catalogs currently expose no per-model effort matrix;
  `unknown` is honest and effort requests on them are rejected.
- Catalog visibility requires a verified runner snapshot; a runner that has
  never verified shows an `ok: false` source with no models.
- Dynamic aliases live only under a configured `alias_prefix`; static preset
  names win on collision (the dynamic entry is suppressed in `/v1/models`).
