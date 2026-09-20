# Jev routing contract — compiler + wrapper boundary

Pinned target: **9Router 0.5.81** (`NINEROUTER_APP` artifact), exercised by
`tests/integration_9router/test_jev_routing_contract.py`.

The compiler (`cli_provider_kanban.compiler`) is the only place an ordered
candidate list becomes concrete configuration. The dispatcher never carries a
fallback list; the compiled combo owns the order.

## Combo members — fail closed

`compile_plan` emits for each route a combo `jev.<route>` whose `models` carry
**only eligible members**, in policy order:

- `enabled` is true,
- `requires_canary` is false (canary-verified),
- `capabilities[<capability>]` is true for the route's capability,
- the backend satisfies the compiler-verified driver contract below.

Dropped members are recorded per combo under `dropped` with a reason. A route
with zero eligible members is **held** (`held: true`, empty `models`) — it is
reported in the plan and in `apply_plan`'s `held` list but never written.

A combo is `operational` only when it has members **and**
`gateway.assume_core_guard` is set. Every compiled route is effectful —
native-agent / API tool-loop traffic can mutate at any tier — so the shared
core no-post-dispatch-retry guard attestation (same principal + same Store +
same task) is required for every combo, not a difficulty-tier subset.
`apply_plan` refuses to write **any** non-operational combo before the first
HTTP mutating call.

## Verified driver contract

| driver       | kind          | pinned alias                        | wire model id                    | registry descriptor id                |
|--------------|---------------|-------------------------------------|----------------------------------|---------------------------------------|
| `hermes-api` | `bai`         | `bai/deepseek-v4.1-flash`           | `deepseek-v4.1-flash`            | `bai:deepseek-v4.1-flash`             |
| `hermes-api` | `commandcode` | `commandcode/deepseek-v4.1-flash`   | `deepseek/deepseek-v4.1-flash`   | `commandcode:deepseek-v4.1-flash`     |
| `devin`      | `devin`       | operator-chosen (e.g. `devin/swe-2-max`) | `swe-2-max`                 | `swe-2-max`                           |

- The hermes-api driver rejects any other preset alias with `unknown_preset`
  at preflight; `discover_models` reports the `provider:model-tail`
  descriptor ids above — those are the `PresetConfig.model_id` values the
  core registry verifies.
- The devin driver never compares the preset alias; the pinned supported
  model is exactly `swe-2-max`.
- An enabled+routed backend violating this table is an actionable
  `CompileError` — never a registered combo member. Codex is not a backend
  kind and can never appear.

**Required example-policy edits (owned by the parent, not this slice):** the
checked-in policy fixture uses invented aliases `hermes-api/bai-deepseek-v4.1-flash`
and `hermes-api/cc-deepseek-v4.1-flash`. They must become
`bai/deepseek-v4.1-flash` and `commandcode/deepseek-v4.1-flash` — the driver
rejects the `hermes-api/…` forms.

## Preset fragment

`presets` entries carry real `PresetConfig` fields — `alias`, `runner_ref`,
`model_id`, `task_policy`, `enabled` — and load through `OperatorConfig`
(see `test_presets_loadable_with_runner_map` for a toy config that
validates). `runner_ref` resolves through the operator `runner_map`
(backend id or driver id → runner instance). When the runner mapping or the
contract cannot pin a model id, the entry lands in `presets_advisory` marked
explicitly *NOT loadable configuration* — never silently emitted as config.

## Effort — intent only

MVP execution is static native-agent `auto`. `effort.applied_hints` is
`["auto"]`; the policy's verified `effort_map` is preserved under
`effort.intent` as central metadata. Non-auto hints are refused upstream by
the dispatch owner (`EffortUnsupported`). 9Router has no effort parameter —
none is emitted.

## apply_plan — real management contract

- Target must be a policy `gateway.targets[]` entry of `kind: disposable`
  on explicit `http://` loopback (`127.0.0.1` / `localhost` / `::1`,
  `localhost` pinned to `127.0.0.1` — no DNS escape), parsed via
  `urllib.parse`. The installed service port **20128 is refused** even when
  marked disposable. No production target, ever.
- Credential file is operator-private JSON (`0600`, regular file, owned by
  the current user, not a symlink, size-bounded) — never argv, never logged:
  - `upstream_key` — the wrapper bearer the provider entry forwards;
  - `management_password` — real `POST /api/auth/login` → `auth_token`
    cookie session; **or**
  - `management_cookie` — an existing `auth_token` session value.
  9Router management auth is the dashboard cookie session; there is no
  management Bearer permission.
- Writes: `PATCH /api/settings`, `POST /api/provider-nodes`,
  `POST /api/providers` (binds node id + upstream key), `POST /api/combos`.
- Readback verifies exactly: combo `models` order, provider-node
  `prefix`/`baseUrl`, provider binding `provider` = node id (the app never
  echoes `apiKey` on GET — verified by binding, never printed), settings
  keys. Drift is a `CompileError`, not a warning.
- Management error bodies are never echoed — they can carry the upstream
  key. Errors carry `METHOD path -> HTTP status` only.

## WrapperClient boundary

- `POST /v1/chat/completions` goes to `base_url` (wrapper direct or a
  gateway); `GET /api/v1/runs/{id}`, `POST …/cancel`, `GET
  /api/v1/artifacts/{id}` go to `control_base_url` with
  `control_credential_file` — direct wrapper control, separate auth.
  Defaults: same base, same credential. 9Router has no `/api/v1/runs` — the
  control paths are the wrapper API's, never the gateway's.
- Transport: `http.client`, no redirects (3xx is terminal — urllib's default
  handler would forward `Authorization` off-host), no environment proxy,
  bounded bodies (`max_response_bytes`), whole-response deadline (the socket
  budget is *remaining* time — a drip-feed cannot stretch it), loopback-only
  targets, canonical-id path segments (no traversal).
- `run.cached` in the body is authoritative for replay state — the
  `X-Run-Cached` header is only a fallback since a gateway may drop it.
- Status is normalized to the finite known set; a body that contradicts the
  submitted `task_id`/`workspace_id`/`attempt_id` yields `unknown` — the
  caller never retries on inconsistency. `WrapperHTTPError` preserves
  `run_id`/post-execution state; a missing body is not classified as
  pre-execution. Parse failures raise `WrapperTransportError` with fixed
  safe messages — raw provider bodies may carry secrets.
- No automatic client retry. Upper layers compare expected
  execution/task/workspace values; the run view is surfaced, not attested.
