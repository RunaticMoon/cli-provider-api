# cli-provider-api

A common runtime for official CLI agents: a typed Driver SDK, a standalone
Runner, a SQLite run controller and an authenticated OpenAI-compatible HTTP API.

**This is a mock-only executable alpha** (milestone M1b). Only the synthetic
`mock` driver exists; no real CLI/account is called anywhere in the code or the
tests. Real Antigravity/Devin drivers, real OS isolation, remote mTLS,
Fusion/resume and PTY support are later milestones and are disabled.

## Components

```
packages/driver-sdk   cli-provider-sdk        typed ProviderDriver SDK (cli_provider_sdk)
packages/transports   cli-provider-transports bounded NDJSON codec + stdio transport
drivers/mock          cli-driver-mock         synthetic driver (entry point cli_provider.drivers)
apps/runner           cli-provider-runner     standalone Runner over a private Unix socket
packages/core         cli-provider-core       operator config, SQLite store, registry, controller
apps/api              cli-provider-api        authenticated HTTP API
```

The API process never imports a driver package. Runners are reached only through
the validated UDS client session; only the Runner loads allowlisted drivers.

## Setup

```bash
uv sync --all-packages
```

Python >= 3.11. `uv.lock` pins the resolved dependency set.

## Quick start (local, mock)

The raw API key is **never** passed as a command-line argument. It is generated
into a `0600` local file and read from stdin or that file.

```bash
umask 077
mkdir -p ./runtime
# 1. Generate a local API key into a 0600 file (the value is not echoed).
uv run python -m cli_provider_api new-key --out ./runtime/local.key
# 2. Hash it from the file and paste the hash into config.yaml.
KEY_HASH="$(uv run python -m cli_provider_api hash-key < ./runtime/local.key)"
echo "put this in config.yaml: $KEY_HASH"

# 3. Copy the example config and paste KEY_HASH in.
cp config.example.yaml config.yaml

# 4. Start the Runner (synthetic mock driver, dev only).
CLI_DRIVER_MOCK_BEHAVIOR=success uv run python -m cli_provider_runner serve \
  --socket ./runtime/runner.sock --instance-id runner-local-1 \
  --driver-id mock --distribution cli-driver-mock --version 0.1.0

# 5. In another shell, start the API.
uv run python -m cli_provider_api serve --config config.yaml

# 6. Use it. curl reads the credential from a 0600 header file, so the key is
#    not placed in curl's argv (visible via `ps`).
install -m 600 /dev/null ./runtime/auth.header
printf 'Authorization: Bearer %s\n' "$(cat ./runtime/local.key)" \
  > ./runtime/auth.header
chmod 600 ./runtime/auth.header
curl -s http://127.0.0.1:8080/health/ready
curl -s http://127.0.0.1:8080/v1/models -H @./runtime/auth.header
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H @./runtime/auth.header -H 'Content-Type: application/json' \
  -d '{"model":"mock/text","messages":[{"role":"user","content":"hello"}],
       "metadata":{"task_id":"task-1","workspace_id":"ws-alpha"}}'
```

`hash-key` also accepts `--key-file PATH` or `--key-env NAME`; there is no
`--key <value>` form.

The API binds to loopback by default and runs a single instance.

## API

- `GET /health/live`, `GET /health/ready` (an unauthenticated readiness probe
  returns only `{"status": "ready"|"not_ready"}`; the detailed runner/preset
  topology requires a valid API key)
- `GET /v1/models`, `POST /v1/chat/completions`
- the same under `/providers/{driver_id}/v1/...`, scoped to that driver's
  manifest `driver_id` (generic; no per-driver code in the API)
- `GET /api/v1/runs/{run_id}`, `GET /api/v1/runs/{run_id}/events`,
  `POST /api/v1/runs/{run_id}/cancel`, `GET /api/v1/artifacts/{artifact_id}`

Every model/run/artifact route requires `Authorization: Bearer <api-key>`.
Requests carry required `metadata.task_id` and `metadata.workspace_id`; the
caller identity comes from the API key only.

`POST /v1/chat/completions` returns a standard `chat.completion` object (never a
202) plus:

- the `X-Run-Id` response header,
- a normalized `run` extension with task/attempt/preset/runner, `status`,
  `outcome`, `verification`, `usage` provenance and `artifact` ids, and
- a standard completion `id` deterministically bound to the run as
  `chatcmpl-{run_id}` (live, cached and every SSE chunk). This lets a client
  identify and cancel the in-flight run even when a gateway drops the initial
  metadata-only SSE chunk or the custom header. Cached replays reuse the same
  bound id rather than minting a new one per HTTP call.

`stream: true` returns SSE `chat.completion.chunk` frames containing **only**
`message.delta` answer text, then a finish chunk and `data: [DONE]`. Internal
tool/planning/permission events are never streamed. Unknown usage is `null`,
never zero.

Because a gateway may drop custom response headers, the run's identity and
normalized outcome are also carried as a JSON `run` extension: the **first**
chunk carries the run id/task/attempt/preset metadata (never answer text) and
the **final** chunk repeats the full normalized run view (status, outcome,
verification, artifact ids, `cached`). This also applies to cached streams.

Requests may carry only `metadata.task_id` and `metadata.workspace_id`. The task
policy is operator-owned and is **rejected** if a request tries to set it
(`metadata.task_policy` is an unknown field).

MVP is text messages only: `tools`, `tool_choice`, images/multimodal content,
sampling parameters and unknown/execution-selecting fields are rejected with a
structured `unsupported_capability` / `invalid_request_error` **before** any
Runner effect.

## Operator configuration

Strict YAML/JSON, validated on load. See `config.example.yaml`. It owns:
`runners` (instance id, socket, expected driver id/version), `presets` (public
slash alias → runner + exact model binding + task policy), `workspaces`,
`principals` (API-key **hashes**, allowed presets/workspaces, concurrency),
`data_dir` and bounded `limits`/`concurrency`.

A missing or invalid binding is a startup error — there is no default-model
substitution. An `enabled` runner/preset only becomes available after a
successful manifest/probe/model verification over the Runner socket, validated
against the SDK schemas.

A driver's manifest/probe/model payloads are validated with
`DriverManifest`/`ProbeReport`/`ModelDescriptor`; a schema failure refuses the
runner. A preset is available only when its discovered model's verification
status is `passed`, or — for a `synthetic` driver only — the preset explicitly
sets `allow_synthetic_unverified: true`. That development opt-in serves the
synthetic model **without** claiming real verification (`real_verification:
false` in `/v1/models` and the authenticated health detail). Effective
capabilities (declared + probed + policy) are exposed per model; `streaming:
none` or `roles: unsupported` presets are refused rather than served.

Admission is bounded per runner and per principal (`per_runner` +
`max_queued_per_runner`, `max_concurrency` + `max_queued_per_principal`).
Excess work is refused **before** any task/attempt is allocated with a
structured `429 queue_full` and has no Runner effect. Request-body reads are
bounded by one fixed `request_body_timeout_seconds` deadline (byte caps do not
bound a slow drip feed), and total request headers are bounded by
`max_headers_bytes` (`431` over the limit).

## Tests

The default suite uses synthetic drivers. The optional real, isolated 9Router
integration requires a trusted **0.5.75** npm app directory; no installed service
or real provider account is used:

```bash
NINEROUTER_APP=/path/to/9router-0.5.75/package/app \
  uv run --all-packages pytest tests/integration_9router -v
```

Latest local verification: **227 default tests + 2 opt-in gateway tests passed**
on Linux/aarch64, Python 3.11. The latter exercise pre-execution fallback,
task identity, artifacts/cache, and early streaming run identification.

```bash
uv run pytest                     # 227 passed (mock-only, no real CLI)
uv run pytest apps/api            # real API subprocess + real Runner subprocess
```

`apps/api/tests/conftest.py` provides `MockSystem`, a helper that starts and
stops the complete mock system (a real Runner subprocess plus a real API
subprocess over a Unix socket and an HTTP port) with a generated local key.

## Known limitations (honest)

- Mock-only alpha: the only driver is the synthetic `mock` driver; all run
  evidence is labelled `synthetic` and is not proof of native compatibility.
- No real Antigravity/Devin drivers, no ACP/PTY transports, no Fusion, no
  session resume, no remote/mTLS Runners, no OS sandbox claims.
- No provider/model fallback or retry: one enabled preset per request, and only
  pre-execution errors could ever be eligible for upstream fallback.
- Single API instance over SQLite; no Redis/Celery/Kubernetes.
- A Runner is quarantined after an unconfirmed/unknown execution and is **not**
  auto-cleared by a successful probe or an API restart; reconciliation and
  process supervision remain explicit operator work.
- Code/review/search workspace isolation and patch collection are later
  milestones; the registered workspace is touched only as an opaque ID here.

See `M1B.md` for the slice report and the exact verified commands.
