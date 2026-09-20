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
`model_alias_mismatch` before spawn. `discover_models()` returns both pins
with `VerificationStatus.UNKNOWN` — they are operator pins, not an
authenticated catalog check.

Two provider config shapes:

- `bai` uses a named `providers.bai` entry in the task-local config
  (Hermes `custom` profile → top-level `reasoning_effort` on the wire).
- `commandcode` uses the **bundled** `commandcode` provider profile. The
  config pins `model.provider`/`model.base_url` so the built-in profile —
  including its DeepSeek `thinking` controls and `reasoning_content` echo —
  stays in charge. It is NOT routed through the generic custom profile.

## Hermes invocation

One process per run (verbatim argv; `env` prepends `HERMES_HOME` because the
executor contract has no per-spawn env):

```
env HERMES_HOME=<task-home> hermes chat \
  --query-file <task-home>/query.txt --oneshot \
  --provider <preset.provider_name> --model <preset.model_id> \
  --reasoning <low|high|max> --toolsets file,terminal \
  --format stream-json --in <workspace> \
  --max-turns <N> --run-budget <seconds> --yolo --ignore-rules
```

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
- `discover_models()` → `VerificationStatus.UNKNOWN` for both pins.
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

## Tests

- `tests/test_hermes_api_driver.py` — unit suite against a fake Hermes
  executable (protocol, presets, isolation, redaction, cancellation,
  bounds). No network.
- `tests/test_hermes_api_integration.py` — real installed Hermes against a
  loopback OpenAI-compatible mock (`tests/fixtures/openai_mock.py`) for
  both presets: asserts an actual `write_file` change, clean NDJSON, the
  pinned model on the wire, `reasoning_effort`, `reasoning_content` echo on
  tool-call turns, and no tool/reasoning leakage into answer text.

No live provider inference is performed by this test suite; provider keys
are synthetic.

## Runner handoff

`execute()` and `probe()` need `ctx.executor` (a `ProcessExecutor`) and
`ctx.permissions` allowing `hermes.yolo`; the current Runner constructs
`RuntimeContext` without an executor. Runner-side injection (an executor
bound to the operator environment — the provider key arrives through that
env, never through request fields) is a parent-worktree task; the shared
Runner/SDK were not modified here.
