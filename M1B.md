# M1B — API + RunController vertical slice report

Mock-only executable alpha. Only the synthetic `mock` driver exists; no real
provider CLI/account or external inference is used by the tests. Local HTTP
and UDS communication is real. All driver run evidence is labelled `synthetic`. This report covers the M1b
API/core slice and its first bounded fix cycle; it is not a native-compatibility
claim.

## Layout added in M1b

```
packages/core   cli-provider-core   operator config, SQLite store, runner registry, RunController
apps/api        cli-provider-api    authenticated OpenAI-compatible HTTP API + SSE
```

The API process never imports a driver package. Runners are reached only through
the validated UDS client session; only the Runner loads allowlisted drivers
(proven by `apps/api/tests/test_isolation.py`).

## Implemented behaviour

- Strict operator config (`runners`, `presets` with exact model binding + task
  policy, `workspaces`, key-hash `principals`, bounded `limits`/`concurrency`).
  Nothing in config is selectable by an HTTP request; a missing/invalid binding
  is a startup error.
- Registry verification over UDS: manifest/probe/model payloads are validated
  against the SDK schemas; the expected driver id/version and SDK version must
  match; a preset is available only when its discovered model verification
  status is `passed`, or — for a `synthetic` driver only — the preset sets
  `allow_synthetic_unverified: true`. The opt-in covers only `unknown`/`not_run`
  (an explicit `failed` is refused even for a synthetic driver) and never claims
  real verification (`real_verification: false`). Effective capabilities
  (declared + probed + policy) are exposed per model; `streaming: none` and
  `roles: unsupported` are refused rather than served as native.
- Truthful per-attempt provenance: the verified Runner manifest's `synthetic`
  value is persisted on the attempt at reservation (with an additive migration
  for an existing M1 DB) and surfaced as `synthetic` in the run view on the
  status, non-stream and SSE surfaces — never a hard-coded default.
- SQLite store: atomic reserve, one active/unknown attempt per caller+task,
  ordered durable events, artifacts, quarantine, restart reconciliation.
- RunController lifecycle: `queued → starting → running →
  completed/failed/cancelling → cancelled`, with `unknown` for genuinely
  unresolved execution. `status`, `outcome` and `verification` stay separate; a
  completed run may be `partial`; unknown usage is `null`, never zero.
- Non-blocking event fan-out: every accepted event is stored durably first, then
  a wakeup is signalled. Reads (SSE, events API) replay from the store, so a slow
  or absent subscriber can never stall a run and a late subscriber still gets the
  full ordered stream. A page shorter than the read limit is not treated as
  "nothing left": the reader re-checks the durable backlog before ending, so a
  run that persists its final events (and reaches its terminal state) while the
  reader is yielding an earlier page is still delivered completely. End
  detection uses the durable terminal status/completed task, not a droppable
  sentinel.
- Streaming frame timeout follows the run's actual deadline + cancel budget. An
  explicit operator `run_frame_timeout_seconds` may tighten that per-frame
  timeout but never exceed the finite deadline contract; control RPCs keep a
  short timeout. A run silent for >15 s inside a larger authorized deadline, or
  waiting behind another run, is not turned into `unknown`.
- Cancellation: `requested` and `confirmed` are separate facts; confirmation
  comes from a validated terminal, not a request ACK. A genuine
  completed/failed/cancelled terminal is never downgraded by a late cancel, and
  a cancel landing in the queued→starting window is escalated to a real driver
  cancel instead of being dropped.
- Bounded admission per runner and per principal (`per_runner` +
  `max_queued_per_runner`, `max_concurrency` + `max_queued_per_principal`):
  excess work is refused **before** any task/attempt is allocated with a
  structured `429 queue_full` and no Runner effect. Effective per-runner
  concurrency is clamped to the capacity the Runner verifies over its `runtime`
  RPC (one serial slot), so excess work waits in the core's bounded queue under
  `queue_timeout_seconds` instead of the Runner's opaque one. Effective
  per-principal concurrency is `min(api.concurrency.per_principal,
  principal.max_concurrency)`, applied to both admission accounting and the
  in-flight semaphore (the global setting is never dead).
- Result-artifact persistence is non-fatal: a filesystem/store failure after a
  validated completed/partial terminal records a bounded, path-free detail and
  leaves the status/outcome intact (no `unknown` downgrade, no quarantine, and
  no artifact claim in the response).
- Runner `run`-RPC errors preserve their code/retryable/stage. A proven
  pre-execution rejection (`QUEUE_FULL`/`INVALID_PARAMS`/`RUN_ALREADY_ACTIVE`)
  is a `failed`/`rejected` run with no quarantine; only a failure after events
  may have had an effect becomes `unknown` + quarantine. Status is monotonic: a
  cancel is never regressed to `running`, and a later validated terminal for the
  same run reconciles its quarantine.
- Bounded request reading: one fixed whole-body deadline
  (`request_body_timeout_seconds`, never renewed per chunk) on top of the byte
  cap, and a total request-header bound (`max_headers_bytes` → `431`).
- SSE run identity: the first `chat.completion.chunk` carries the run metadata
  (id/task/attempt/preset) in a JSON `run` extension and the final chunk repeats
  the normalized run view (status/outcome/verification/artifact ids/`cached`),
  because a gateway may drop custom headers. Cached streams do the same. Only
  `message.delta` is ever answer content.
- Deterministic completion id: the standard `id` on every response and chunk is
  `chatcmpl-{run_id}` (live, cached, non-stream and SSE). Because the pinned
  gateway can drop the initial metadata-only chunk, the bound id is what lets a
  caller identify/cancel the in-flight run from standard OpenAI fields; cached
  replays reuse the same id.
- Operator CLI: `new-key --out PATH` writes a `0600` local key file; `hash-key`
  reads the key from stdin, `--key-file`, or `--key-env`. A raw key is never a
  command-line argument.
- Runner UDS socket is created under a narrowed umask so it is `0600` from the
  first instant, even with a permissive caller umask; the previous umask is
  restored.
- Unauthenticated `/health/ready` returns only a status; the internal topology
  detail requires a valid API key. Model/run/artifact routes and ownership are
  unchanged.

## Verification

```bash
uv run pytest                 # 315 passed (fixture-only; no real CLI/account)
uv run pytest packages/core   # config/store/controller/registry/runner session
uv run pytest apps/runner     # real Runner subprocess + real Unix socket
uv run pytest apps/api        # real API subprocess + real Runner subprocess
uv run pytest drivers/mock    # mock driver behaviours (incl. many_events)
```

Focused regressions include: injected ENOSPC/EACCES/store artifact-persistence
failures retaining a validated completed/partial terminal with no quarantine,
the global `per_principal` cap enforced as `min(global, principal.max)` with
independent principals, a synthetic `failed` verification refused even under the
opt-in, persisted per-attempt `synthetic` provenance plus an old-M1-DB
migration, a 300-event non-stream run with no subscriber, a
late SSE subscriber receiving the full ordered stream, a partial first page
with events appended while the reader yields (deterministic replay regression),
both cancel races,
a run silent beyond 15 s inside a larger valid deadline, a second run queued
behind >5 s of real UDS work completing without a false `unknown`/quarantine,
per-runner dispatch clamped to the verified Runner `runtime` capacity, task-policy
override rejection, message-level execution fields rejected before execution
(and missing usage counts kept `null`, never `0`), typed pre-execution Runner
rejections not quarantining while post-event failures do, monotonic status after
cancel, malformed manifest/probe/model payloads, synthetic opt-in without real
verification, `streaming=none` refusal, restrictive socket creation, header/body
bounds, admission floods with capacity recovery and independent principals, and
first/final/cached SSE run metadata.

## Evidence classification

- **Implemented**: SDK, transports, mock driver, Runner, core, API, SSE, store,
  registry, operator CLI.
- **Fixture-tested**: the 315-test suite above, including real subprocess +
  UDS + HTTP boundaries. No native CLI or account is used.
- **Native-tested**: none. The mock reports `verification=not_run` and
  `usage=unknown`; a terminal event proves the driver finished, not that any work
  was verified.
- **Gateway-tested**: 2 opt-in tests with real isolated 9Router 0.5.75 → API
  → UDS → synthetic Mock Driver passed separately. They do not establish native
  CLI coding, safe post-execution fallback, or OS isolation.
- **Pending/not run**: real Antigravity/Devin drivers, real OS isolation,
  remote/mTLS Runners and Fusion/resume/PTY are later milestones.

## Limitations

- A Runner is quarantined after an unconfirmed/unknown execution. A later
  validated terminal for the *same* run reconciles the matching quarantine, but
  a probe or an API restart does not clear it.
- Single API instance over SQLite; no Redis/Celery/Kubernetes.
- No provider/model fallback or retry; the logical request hash is independent
  of the model so a future pre-execution fallback can reuse it.
- Workspace/patch/evidence and real OS isolation are later milestones; the
  registered workspace is an opaque ID here.
