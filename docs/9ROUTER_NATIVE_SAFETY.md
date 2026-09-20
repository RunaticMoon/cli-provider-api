# 9Router native retry safety + execution metadata contract

Scope: `feat/jev-router-safety` on `cli-provider-api`. Owner slice:
`packages/core/**`, `apps/api/**`, `tests/integration_9router/**`, this doc,
plus backwards-compatible SDK fields in `packages/driver-sdk/.../models.py`.
Runner / transports / native drivers / kanban are owned elsewhere and are NOT
modified here.

## The defect this fixes

`RunController.submit` used to block only ACTIVE/UNKNOWN attempts and replay
COMPLETED ones. A terminal `failed` or `cancelled` attempt allowed a brand new
attempt under the same `(principal, task_id)` — so a sequential-fallback
gateway (stock 9Router `comboStrategy: fallback`) could silently execute the
same task twice: once on candidate A (with real effects), then again on
candidate B.

## Durable cross-candidate admission guard

Admission is decided inside `Store.reserve()` under `BEGIN IMMEDIATE` —
the same transaction that inserts the new attempt — so concurrent retries
cannot race past it (`store.py::_BLOCKER_PREDICATE`).

A new attempt for `(principal, task_id)` is admitted **iff every prior attempt
is proven pre-execution**, where "proven pre-execution" is exactly:

- `failed` with `outcome IN ('rejected', 'queue_timeout')` — the queue
  admission bound or the Runner's typed pre-dispatch rejection codes
  (`QUEUE_FULL`, `INVALID_PARAMS`, `RUN_ALREADY_ACTIVE`), or
- `cancelled` with `started_at IS NULL` — cancelled while queued, never
  dispatched.

`started_at` is the durable dispatch marker: it is persisted atomically in
`_mark_starting` *before* the run RPC/effect boundary, not after. Everything
else is a permanent blocker for the task:

- `completed` / `partial` (cached replay only — original preset provenance is
  preserved, a different preset is `model_conflict`, never a re-execution)
- `failed/provider_error` (test failures and quality failures included —
  they are not provider fallback conditions)
- `cancelled` after dispatch
- `unknown` (transport-uncertain: dropped response, timeout, restart
  reconciliation, mid-stream error)
- any communication failure after possible execution

HTTP mapping (unchanged semantics): the blocked retry is `409` with
`error.code == "run_not_retryable"` (or `unknown_attempt` for an unresolved
attempt) and `error.run_id` naming the original attempt for control readback.
A gateway that retries anyway gets a second refusal — the second candidate
never reaches a Runner at all.

Guarantee proven by the fixture evidence: `effects.ndjson` (agent starts)
never grows past 1 for a task that already executed, whether the gateway sends
the second HTTP request or not.

## Same-principal precondition

The lock is `(principal, task_id)`. For a gateway fallback set to be guarded,
every candidate MUST resolve to the same API principal (same bearer key) and
the same Store. Per-driver fallback state does not exist and is not permitted.
Cross-model submissions under the same task hit the identical lock
(`test_retry_safety.py`, both suites).

## Execution metadata (`metadata.execution`)

Optional, single nested object, complete-if-present, all four bounded scalar
fields required together:

```json
"metadata": {
  "task_id": "…", "workspace_id": "…",
  "execution": {
    "task_revision": "rev-7",
    "base_revision": "base-2026.09",
    "route": "worker.code.standard",
    "policy_version": "pol-3"
  }
}
```

- `task_id` + `workspace_id` remain required; principal identity is the
  authenticated key, workspace mapping is authorization. Neither is ever
  caller-trusted for execution.
- `route` must be `role.capability.tier` (three bounded dot segments — never
  a path, URL, executable or rights selector). Unknown keys, missing fields,
  path-shaped values, oversized scalars and non-strings are rejected with
  `400 invalid_request` before any reservation exists.
- `run_id` / `attempt_id` are wrapper-generated and canonical; they cannot be
  supplied or overwritten by request metadata.
- The context participates in the request hash: a replay under the same task
  id with mutated context is `task_content_conflict`; a request without it
  keeps the legacy digest.
- It is persisted verbatim on the attempt (`attempts.execution`, migrated
  additively), carried into the Runner run params, echoed on the normalized
  run view, and preserved through `NormalizedRequest.execution` to the worker.
- No execution `tool_calls` are returned to Hermes; only `message.delta` is
  answer text.

### Runner integration

`cli_provider_runner.protocol.RunParams` now accepts the SDK's optional typed
`ExecutionContext` and preserves it in `to_driver_request`. All other unknown
fields remain rejected. `apps/runner/tests/test_execution_context_wire.py`
proves both typed driver-request propagation and the real Runner UDS acceptance;
`apps/api/tests/test_execution_metadata.py` proves HTTP/control readback and
same-run cached replay. Before integration these positive tests failed because
`execution` was rejected as an unknown field (recorded RED).

`tests/integration_9router/fixture_runner.py` remains a separate real UDS/NDJSON
stand-in recording context on disk. Those fixture tests prove the actual
9Router metadata and effectful retry boundaries, not native model inference.

## 9Router realities

- Custom response headers (`X-Run-Id`) may be dropped by the gateway; run
  identity is therefore carried in the JSON `run` extension / SSE chunks and
  verified by control readback (`/api/v1/runs/{run_id}`), never by headers.
- A gateway HTTP 200 is not evidence of the underlying execution outcome —
  the `run` extension status/outcome is authoritative.
- Stock fallback retries on failure statuses: that is safe only because the
  admission guard refuses the second attempt durably. Automatic mutating
  combos remain safe **only** under the same-principal precondition above.

## Evidence / tests

- `packages/core/tests/test_retry_safety.py` — admission predicate, reserve
  race, restart reconciliation, marker ordering.
- `apps/api/tests/test_retry_safety.py` — HTTP surface: 409 codes, identity
  preservation, queued-cancel positive control.
- `apps/api/tests/test_execution_metadata.py` — strict schema, rejection of
  unknown/path/id-override fields, hash binding, stock-runner INVALID_PARAMS
  safety.
- `tests/integration_9router/test_retry_safety.py` — real UDS fixture:
  preflight fallback runs once; effectful failure / drop / mid-stream error /
  dispatched cancel / partial / concurrent duplicate all leave exactly one
  agent start; restart keeps the block; metadata reaches the worker.
- `tests/integration_9router/test_gateway_safety.py` — real pinned 9Router
  0.5.81, two same-principal candidates: preflight rejection falls back once;
  effectful failure never starts a second agent even when the gateway retries
  HTTP; metadata propagates through the gateway.
- `tests/integration_9router/test_pinned_gateway.py` — pinned at 0.5.81.

Opt-in: `NINEROUTER_APP=<0.5.81 app dir> uv run --all-packages pytest
tests/integration_9router`. Logs: `/home/ubuntu/ops/kanban-jev/router-*.log`.
