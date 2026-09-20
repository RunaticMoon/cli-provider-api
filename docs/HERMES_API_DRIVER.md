# hermes-api driver — B.AI / CommandCode official APIs via Hermes

`cli-driver-hermes-api` is one `ProviderDriver` that runs the installed
Hermes agent CLI (`hermes chat`) as the tool executor and agent loop, while
pinning exactly one official model-API backend and exact model for the whole
run. Provider choice is external: the outer Combo/9Router layer selects a
wrapper preset; the driver never reclassifies, falls back, or switches tiers
inside a run.

Verified against Hermes `v0.21.3` (upstream `d86a1687`), installed at
`/home/ubuntu/.local/bin/hermes`, source `/home/ubuntu/.hermes/hermes-agent`.

## Presets

| Preset alias                        | Provider  | Base URL                                  | Model id                       | Key env var          |
| ----------------------------------- | --------- | ----------------------------------------- | ------------------------------ | -------------------- |
| `bai/deepseek-v4.1-flash`           | `bai`     | `https://api.b.ai/v1`                     | `deepseek-v4.1-flash`          | `BAI_API_KEY`        |
| `commandcode/deepseek-v4.1-flash`   | `commandcode` | `https://api.commandcode.ai/provider/v1` | `deepseek/deepseek-v4.1-flash` | `COMMANDCODE_API_KEY` |

A request must carry `preset` equal to one of these aliases. If
`model_alias` is supplied it must equal the preset's exact model id (or the
SDK-safe descriptor id `provider:model`); anything else fails with
`model_alias_mismatch` before spawn.

`discover_models()` performs a **bounded authenticated `GET {base_url}/models`**
against each preset's official endpoint with the operator env key
(`BAI_API_KEY` / `COMMANDCODE_API_KEY`), matching the **exact** pinned model
id — never a family slug or alias. A parsed catalog containing the exact id
is `passed`; a parsed catalog without it, or a 401/403, is `failed`; a missing
key, unreachable endpoint, redirect, malformed or oversized body is `unknown`.
Results are cached for `HERMES_API_CATALOG_TTL` (default 300 s). Catalog
presence is membership only — never a quota, billing or entitlement claim.
Redirects are never followed, so the credential is never forwarded
off-origin, and response bodies/headers are never surfaced in verification
reasons.

For fixture deployments only, `HERMES_API_BAI_BASE_URL` /
`HERMES_API_COMMANDCODE_BASE_URL` may replace the endpoint *origin* (the
preset's `base_path` is preserved). These are operator env config — like
`HERMES_API_CLI` — never request-derived, and they are scrubbed from the
child environment. Production defaults remain the official endpoints.

Two provider config shapes:

- `bai` uses a named `providers.bai` entry in the task-local config
  (Hermes `custom` profile → top-level `reasoning_effort` on the wire).
- `commandcode` uses the **bundled** `commandcode` provider profile. The
  config pins `model.provider`/`model.base_url` so the built-in profile —
  including its DeepSeek `thinking` controls and `reasoning_content` echo —
  stays in charge. It is NOT routed through the generic custom profile.

## Hermes invocation

One process per run (shape below; `env` adjusts the child's environment
without a shell because the executor contract has no per-spawn env):

```
env -u <secret/provider env names> HERMES_HOME=<task-home> hermes chat \
  --query-file <task-home>/query.txt --oneshot \
  --provider <preset.provider_name> --model <preset.model_id> \
  --reasoning <low|high|max> --toolsets file,terminal \
  --format stream-json --in <workspace> \
  --max-turns <N> --run-budget <seconds> --yolo --ignore-rules
```

Every environment variable whose name looks secret-shaped or provider-owned
(`*_API_KEY`, `*TOKEN*`, `*SECRET*`, `*AUTH*`, `DEVIN_*`, `OPENAI_*`, …) is
unset in the child via `env -u` — **names only, values never appear in
argv**. The one exception is the *selected* preset's `key_env`: the child
needs exactly that one backend's key. The operator's own `HERMES_HOME` is
also unset and then reassigned to the task home, so it cannot leak through.
This is best-effort hygiene, not a sandbox.

`--safe-mode` and `--ignore-user-config` are deliberately **not** passed:
both bypass the generated task-local provider config.

## Task-local isolation

Every run creates a fresh `HERMES_HOME` (`mkdtemp`, mode 0700; under
`HERMES_API_STATE_DIR` when set) containing:

- `config.yaml` (0600) — emitted as JSON (a YAML-1.2 subset, never
  string-interpolated). It disables `compression`, `auxiliary`
  title generation, memory, skills autoload, curator, plugins, MCP servers,
  `fallback_providers`, external auth adoption and tirith; sets
  `model.reasoning_echo: true` (DeepSeek `reasoning_content` must be echoed
  on tool-call turns) and `agent.api_max_retries: 1`.
- `query.txt` (0600) — serialized request messages.

The API key reaches the native client only via the operator-bound process
environment. The config carries the env var **name** (`key_env`), never the
value. No secret is placed in argv, query text, logs, or event payloads;
provider/driver error text is scrubbed (`_SECRET_RE` plus literal
env-value replacement) and truncated to 240 chars before it can reach an
event.

## Reasoning vocabulary

Only native values `low`, `high`, `max` are accepted (default `low`;
operator override via `HERMES_API_REASONING` or the `reasoning` constructor
argument). Anything else fails `unsupported_reasoning` before spawn —
internal hints (`auto`, `economy`, `balanced`, …) and unverified levels
(`medium`, `xhigh`, …) are never passed through.

Configured/applied vs unobserved: the driver applies the level on the wire
request (verified on the loopback mock: `reasoning_effort: high`; the
CommandCode profile additionally emits `thinking: {type: enabled}`). The
completion message reports it as configured-and-applied; provider-side
billing/quota effects are unobserved and are never claimed.

## Stream-json interpretation

Discriminated by `type`, five records only:

- `system`/`init` — first frame; `model` must equal the preset pin or the
  run fails `model_mismatch`. Any record before init → `protocol_error`.
- `text` — the only records forwarded as `MessageDeltaEvent` answer text.
- `tool_use` / `tool_result` — mapped to `ToolStartedEvent` /
  `ToolCompletedEvent`; never emitted as answer text.
- `result` — the authoritative terminal record. Nonzero `exit_code` or an
  `error` field → `run.failed` (`cli_reported_error`, redacted). A clean
  process exit without a `result` record is `missing_result`, not success.

Any frame outside the protocol → `unknown_event` and forced termination.

## Policy gate

`--yolo` grants the CLI unrestricted file/terminal tools, so `execute()`
requires the runtime permission service to allow `hermes.yolo`
(`YOLO_ACTION`) and a finite `deadline_seconds`; both are checked before
spawn. `ctx.executor` must supply the process executor — see *Runner
handoff* below.

Before any effect (task home, config write, spawn), `execute()` re-verifies
the preset's catalog membership through the same bounded `GET /models`
check — a long-lived discovery/Registry cache must never turn a stale
listing into a standing authorization. The result rides the TTL cache
(`HERMES_API_CATALOG_TTL`), so repeated runs within the TTL reuse it; a lost
membership, auth rejection or unreadable catalog fails the run with
`catalog_not_verified` before the CLI exists.

## Bounds, cancellation, cleanup

- Process lifetime: driver watchdog = `min(deadline_seconds,
  HERMES_API_RUN_BUDGET cap)`; the same value is passed to Hermes as
  `--run-budget`. A bound trip is reported as `run.cancelled`, never as a
  provider failure.
- Frames bounded by `max_frame_bytes` (transport default); stderr bounded
  and classified by `NdjsonProcessTransport`.
- `cancel(run_id)` terminates the CLI process group through the transport;
  surviving group members are reported, not silently chased. No process is
  ever restarted after a side effect.
- `run.completed` outcome is `succeeded`, or `partial` when any
  `tool_result` reported `is_error`. Task success is NOT independently
  verified — tool execution and file changes belong to the trusted task
  policy.

## Usage / verification limits

- `tokens` in the terminal `result` maps to `Usage(REPORTED)`; all-zero or
  absent → `Usage(UNKNOWN)`. Quota and actual charge are always unknown.
- `discover_models()` reports the bounded catalog check honestly
  (`passed`/`failed`/`unknown`); membership is never a quota or entitlement
  claim.
- The driver does not claim provider-side billing verification.

## Operator environment

| Env var                       | Default   | Meaning                                   |
| ----------------------------- | --------- | ----------------------------------------- |
| `HERMES_API_CLI`              | `hermes`  | CLI executable                            |
| `HERMES_API_REASONING`        | `low`     | `low`/`high`/`max` only                   |
| `HERMES_API_MAX_TURNS`        | `40`      | `--max-turns`                             |
| `HERMES_API_RUN_BUDGET`       | `600`     | cap on `--run-budget` seconds             |
| `HERMES_API_STATE_DIR`        | unset     | parent dir for task-local `HERMES_HOME`s  |
| `HERMES_API_EXPECTED_VERSION` | unset     | probe fails unless CLI version matches    |
| `HERMES_API_CATALOG_TTL`      | `300`     | seconds a `/models` result may be cached  |
| `HERMES_API_CATALOG_TIMEOUT`  | `10`      | bound on the catalog HTTP call            |
| `HERMES_API_BAI_BASE_URL`     | official  | fixture-only origin override (scrubbed)   |
| `HERMES_API_COMMANDCODE_BASE_URL` | official | fixture-only origin override (scrubbed) |

## Tests

- `tests/test_hermes_api_driver.py` — unit suite against a fake Hermes
  executable plus a loopback `/models` catalog fixture (protocol, presets,
  isolation, redaction, cancellation, bounds, catalog verification and its
  failure modes). No real endpoint is contacted.
- `tests/test_hermes_api_integration.py` — real installed Hermes against a
  loopback OpenAI-compatible mock (`tests/fixtures/openai_mock.py`) for
  both presets: asserts an actual `write_file` change, clean NDJSON, the
  pinned model on the wire, `reasoning_effort`, `reasoning_content` echo on
  tool-call turns, and no tool/reasoning leakage into answer text.
- `apps/runner/tests/test_hermes_wiring.py` — real Runner subprocess over
  UDS driving this driver: bound workspace + `hermes.yolo` grant completes;
  missing grant, missing config and lost catalog membership all fail before
  any spawn; one skipped-when-absent case runs the real `hermes` binary
  through the Runner against the loopback provider.

No live provider inference is performed by this test suite; provider keys
are synthetic.

## Runner handoff

The standalone Runner binds the runtime context from an operator-only
`serve --execution-config FILE` (a protected, validated JSON file — never a
request field). For an effectful (non-synthetic) driver like this one the
run is refused before any driver code executes when the request's
`workspace_id` has no binding. A bound run gets:

- `ctx.executor` — the runner's process executor (provider key arrives
  through that environment, never through request fields);
- `ctx.workspace` — the bound workspace root (driver uses it for `--in` and
  `cwd`);
- `ctx.permissions` — the bound allow-list; `hermes.yolo` must be granted
  there or the run fails `yolo_not_preapproved` before spawn.

Binding denial happens before task-home creation or process spawn, and the
bound workspace is claimed serially across runner processes for the life of
the run. This is dependency injection, not an OS sandbox: the spawned CLI
can still reach the host outside the workspace.
