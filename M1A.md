# M1A — executable Driver SDK / Mock Runner vertical slice

Development/mock only. This slice is **implemented and fixture-tested** against a
real runner subprocess. It is **not** native-tested against any real CLI and does
not call Antigravity/Devin/commandcode or touch any account/auth material.

> **Historical slice report (M1a only).** The workspace now also contains the
> M1b API/core packages (`packages/core`, `apps/api`). See `M1B.md` and
> `README.md` for the current tree and the full-suite count. The statements below
> about the API/core slice not being scaffolded, and the per-slice test count,
> describe the M1a slice at the time it was written, not the current workspace.

The API/core slice is intentionally **not scaffolded here**.

## Layout

```
pyproject.toml            uv workspace root (package = false)
uv.lock                   locked workspace resolution
packages/driver-sdk       cli-provider-sdk       (import: cli_provider_sdk)
packages/transports       cli-provider-transports (bounded NDJSON codec + stdio)
drivers/mock              cli-driver-mock        (entry point: cli_provider.drivers)
apps/runner               cli-provider-runner    (standalone UDS RPC runner)
```

Separate installable packages, `src/` layout, one shared isolated `.venv`.

## Setup (isolated, no global installs)

```bash
uv sync --all-packages
```

Python >= 3.11, Pydantic 2, asyncio. `uv.lock` is checked in; the venv is
`.venv/` and is gitignored. Run commands below through the workspace venv
(`uv run python ...` or `.venv/bin/python ...`).

## Run the tests

```bash
uv run pytest                 # M1a-era slice count: 99 passed (see README)
uv run pytest packages        # SDK + transports
uv run pytest drivers/mock    # mock driver behaviours
uv run pytest apps/runner     # real subprocess + Unix socket
```

Every `apps/runner` test launches a real `cli-provider-runner serve` subprocess
and speaks the JSON protocol over a real Unix-domain socket (some cases also run
an in-process server over a real socket for driver injection). Direct
function-level tests exist too, but they are not the boundary evidence.

Real-subprocess/UDS coverage: success, completed-with-partial, explicit failure,
cancel (requested vs confirmed), queued-cancel without driver start, mid-stream
deadline (hang), events that cannot renew the deadline, ignored cancellation →
`unknown`, abrupt driver crash → `unknown`, malformed sequences (duplicate /
event-after-terminal / no terminal) → `unknown`, oversize frame, malformed JSON
frame, unknown method, run-request attempting to select an executable/cwd, abrupt
client disconnect (graceful and mid-stream) recovery, bounded-queue rejection,
duplicate active run id, unknown driver id, distribution mismatch, version
mismatch, and bad-manifest fail-closed.

## Manual end-to-end demo

```bash
SOCK=/tmp/m1a-demo.sock
CLI_DRIVER_MOCK_BEHAVIOR=partial uv run python -m cli_provider_runner serve \
  --socket "$SOCK" --instance-id demo-1 \
  --driver-id mock --distribution cli-driver-mock --version 0.1.0 &

uv run python -m cli_provider_runner manifest --socket "$SOCK"
uv run python -m cli_provider_runner run --socket "$SOCK" \
  --run-id r1 --task-id t1 --attempt-id a1 --preset agy/review --workspace-id w1 \
  --message hello

kill -TERM %1     # SIGTERM triggers cleanup; the socket is unlinked
```

Verified output (abridged): the ready line goes to stderr; `run` streams
`run.started`, three `message.delta`, then exactly one `run.completed` carrying
`outcome: "partial"`, and the response result is
`{"status":"completed","outcome":"partial","verification":{"status":"not_run",...},"usage":{"provenance":"unknown","input_tokens":null,"output_tokens":null},"terminal_kind":"run.completed","terminal_sequence":5,...,"synthetic":true}`.

## Public SDK surface (exact)

`cli_provider_sdk.types` exposes exactly:

```
CancelResult, DriverManifest, ModelDescriptor, NormalizedRequest, ProbeReport,
RunEvent, RuntimeContext
```

These are the canonical validated models (no parallel unvalidated shapes). The
driver Protocol is:

```python
class ProviderDriver(Protocol):
    @property
    def manifest(self) -> DriverManifest: ...
    async def probe(self, ctx: RuntimeContext) -> ProbeReport: ...
    async def discover_models(self, ctx: RuntimeContext) -> list[ModelDescriptor]: ...
    def execute(self, request: NormalizedRequest, ctx: RuntimeContext) -> AsyncIterator[RunEvent]: ...
    async def cancel(self, run_id: str, ctx: RuntimeContext) -> CancelResult: ...
    async def aclose(self) -> None: ...
```

`BaseDriver` is the convenience ABC. A driver written directly against this
Protocol runs without any API-specific adaptation: the suite defines such a
driver and drives it through the real UDS boundary.

Context services (`ProcessExecutor`, `WorkspaceService`, `PermissionPolicy`,
`Cancellation`, `RedactedLogger`, `SessionStore`) are protocols; injection is
dependency injection, **not** an OS sandbox.

## Events (canonical wire names, schema_version 1)

`run.started`, `message.delta`, `tool.started`, `tool.completed`,
`permission.required`, `artifact.created`, `usage.updated`, `run.completed`,
`run.failed`, `run.cancelled`.

Every event carries `schema_version=1`, `run_id`, a strictly increasing
`sequence`, a timezone-aware `timestamp`, `synthetic` and a kind-specific
validated payload. Terminal kinds are `run.completed` / `run.failed` /
`run.cancelled`. Only `message.delta` carries model-visible answer text; tool,
permission and artifact events are forwarded with their own kind and are never
rewritten as answer deltas. `run.completed` carries an `outcome` of
`succeeded` | `partial`.

Malformed frames or legacy-shaped events are rejected, never repaired.

## Semantics

- `status` (completed/failed/cancelled/unknown), `outcome`
  (succeeded/partial/provider_error/cancelled/unknown) and `verification`
  (passed/failed/not_run/unknown, with source+reason) are separate fields.
  `completed` does not imply `succeeded`: a completed run may be `partial`.
- Unknown usage is `null` (provenance `unknown`), never an invented zero.
- `unknown` never claims a terminal event or a success.
- Capabilities declare `streaming native|buffered|none`, `sessions
  none|explicit_resume|persistent`, `roles native|serialized|unsupported`,
  `structured_output` as a **mode** (`native|validated|none`), plus explicit
  booleans for `external_tool_calls`, `internal_tools`, `vision`,
  `workspace_write`, `web_search` and a `usage` provenance.
- `DriverManifest` requires an explicit `sdk_version` and at least one
  `supported_transports` entry (`stdio|acp|pty`). A missing or invalid manifest,
  or an unsupported SDK version, fails closed (`MANIFEST_INVALID` /
  `SDK_VERSION_UNSUPPORTED`) for both entry-point and injected drivers.

## Aliases

Preset and model aliases are public dotted/slashed names and are **not**
filesystem paths: `mock/text`, `agy/review`, `mock/text:latest`, `a/b/c/d` are
valid. Validation is separate from ID validation and never normalizes; it rejects
empty segments, leading/trailing slashes, traversal segments (`.`/`..`), control
characters, more than 4 segments and over-length values. Run/task/attempt and
workspace IDs stay strict (`ID_PATTERN`, no slashes).

## Deadline, cancellation and queue

- A finite execution deadline is enforced **mid-stream** from run start; the
  effective deadline is `min(request.deadline_seconds or server max, server
  max_run_seconds)`. Streaming events and keepalives cannot renew it.
- On deadline the runner requests a bounded driver cancel. If the driver confirms
  a stop the run is `cancelled`; if it does not, or does not stop within the
  cancel deadline, the run is `unknown`. It is never reported `completed`.
- Cancelling a **queued** run sets a pre-start flag; the driver is never started
  and `events_seen` is 0. `confirmed` is reported only after the run cannot
  start.
- Cancellation reports `requested` and `confirmed` separately with a bounded
  deadline; `confirmed` requires the driver stream to actually end
  `run.cancelled`.
- Per-instance concurrency is 1 with a bounded queue; a full queue is rejected
  (`QUEUE_FULL`). There is **no retry** of unknown/effectful attempts.
- Terminal success is delayed until the driver generator genuinely ends and
  exactly one terminal event is validated.

## Driver entry point / allowlist

Installed distribution `cli-driver-mock` 0.1.0 exposes:

```toml
[project.entry-points."cli_provider.drivers"]
mock = "cli_driver_mock:MockDriver"
```

The Runner loads a driver only when the installed distribution **name**, its
**version** and the **entry-point/driver id** all match an operator allowlist
entry. Name/version are compared *before* `entry_point.load()`, so unknown or
mismatched plugins are refused, not imported. Mock fixture behaviour is
operator-selected at Runner launch via the environment
(`CLI_DRIVER_MOCK_BEHAVIOR` = `success|partial|failed|hang|hang_ignores_cancel|crash|malformed|slow|many_events`,
`CLI_DRIVER_MOCK_MALFORMED_MODE` = `duplicate_sequence|event_after_terminal|no_terminal`),
never by a request.

## Internal Runner wire contract (v1)

Documented so the next API slice can drive a Runner **without importing any
driver**. Source of truth: `apps/runner/src/cli_provider_runner/protocol.py`.
Reusable client: `cli_provider_runner.client.RunnerClient`.

- **Framing**: newline-delimited JSON, one object per line, size-bounded
  (`--max-frame-bytes`). A frame that is not one valid JSON object is rejected;
  malformed JSON is never repaired.

Envelopes (`v` = protocol version, currently `1`):

```
request   {"v":1,"type":"request","id":"<client-id>","method":"<name>","params":{...}}
response  {"v":1,"type":"response","id":"<client-id>","ok":true,"result":{...}}
          {"v":1,"type":"response","id":"<client-id>","ok":false,
           "error":{"code":"...","message":"...","retryable":false}}
event     {"v":1,"type":"event","request_id":"<client-id>","event":{...RunEvent...}}
```

Methods:

| method            | params                                   | result                          |
|-------------------|------------------------------------------|---------------------------------|
| `manifest`        | `{}`                                      | `DriverManifest`                |
| `probe`           | `{}`                                      | `ProbeReport`                   |
| `discover_models` | `{}`                                      | `{"models": [ModelDescriptor]}` |
| `run`             | `RunParams`                               | streamed events + `RunResult`   |
| `cancel`          | `{"run_id": "..."}`                       | `CancelResult`                  |
| `shutdown`        | `{}`                                      | `{"instance_id","stopping"}`    |

`run` answers with zero or more `event` frames followed by exactly one
`response`. `ok:true` means the RPC completed — the run's own `status`/`outcome`
live in `result` and may still be `unknown`.

`RunParams` carries normalized references only: `run_id`, `task_id`,
`attempt_id`, `preset` (alias), `workspace {workspace_id, revision?}`, optional
`model_alias` (alias), `messages[]` and optional `deadline_seconds`.
Unknown/extra fields (e.g. `package`, `cwd`, `env`) are rejected with
`INVALID_PARAMS`.

Error codes: `MALFORMED_REQUEST`, `FRAME_TOO_LARGE`, `UNKNOWN_METHOD`,
`INVALID_PARAMS`, `DRIVER_UNAVAILABLE`, `QUEUE_FULL`, `RUN_ALREADY_ACTIVE`,
`RUN_NOT_FOUND`, `PROTOCOL_ERROR`, `INTERNAL_ERROR`.

## Declared capabilities (mock)

`streaming=native, sessions=none, roles=serialized, structured_output=none`, and
explicitly **false**: `external_tool_calls`, `internal_tools`, `vision`,
`workspace_write`, `web_search`; `usage=unknown`. No arbitrary external
tool_calls support exists in this slice.

## Known limitations (honest)

- No API/core, no HTTP routes, no SSE, no auth, no ownership/idempotency store —
  those are the next slice.
- Only the synthetic mock driver exists. No Antigravity NDJSON or Devin ACP
  driver, and no real CLI/provider inference anywhere; all mock evidence is
  labelled `synthetic`.
- No PTY and no ACP transport. The stdio transport is plain bounded NDJSON and
  claims nothing more.
- The Unix socket is local IPC with `0600` permissions. Process separation here
  is **not** an OS sandbox and no filesystem/network isolation is claimed.
- Deadline semantics are delivered as `cancelled` (stop confirmed) or `unknown`
  (not confirmed); there is no distinct `timed_out` status, and a driver that
  ignores cancellation is abandoned rather than forcibly killed.
- No session resume, no artifact capture, no retry/candidate selection (fallback
  belongs elsewhere), one driver per Runner instance.
- The mock reports `verification=not_run` and `usage=unknown`: a terminal event
  proves the driver finished, not that any work was verified.

## Evidence classification

- **Implemented**: workspace/SDK/transports/mock/runner above.
- **Fixture-tested**: 99 tests, including the real subprocess + UDS suite.
- **Native-tested**: none (no real CLI/account).
- **Blocked/not run**: real Antigravity and Devin protocols, real OS isolation,
  9Router integration — out of scope for M1a.

## Session

Session ID: `81800cdb-f623-4d60-aa5d-3f54fc7606dd`.
