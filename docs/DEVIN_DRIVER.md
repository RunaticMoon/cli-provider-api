# Devin driver (`cli-driver-devin`)

`ProviderDriver` for the officially installed Devin CLI (`devin acp`), speaking
newline-delimited JSON-RPC 2.0 (ACP `protocolVersion` 1) on stdio. Interface
facts are verified observations from `docs/NATIVE_PROTOCOLS.md` and the
operator probe (`ops/kanban-jev/devin-acp-discovery.json`), not guesses.

Status: structurally complete, **not enabled for production runs**. All tests
drive a synthetic fixture executable (`drivers/devin/tests/fixtures/`); no
live inference, OAuth read, private upstream API, or CLI upgrade is performed
anywhere in this slice.

## Supported surface

| Item | Value |
| --- | --- |
| Executable | `devin` (override via `DEVIN_CLI`) |
| Verified distribution | `devin --version` output, e.g. `3000.10.31` |
| Models | exactly `swe-2-max` — the only supported model |
| Effort/tier | none — `swe-2-max` *is* the Max variant; any effort suffix (`:high`, `@low`, `/x`) fails before spawn |
| Protocol | ACP over stdio: `initialize`, `session/new`, `session/set_mode`, `session/prompt`, `session/cancel`, `session/update`, `session/request_permission` |
| Sessions | explicit `session/new` only; no resume/latest-session in this slice |
| Workspace | `cwd` = `RuntimeContext.workspace.root` when bound by the Runner (execution config), else the operator's static `DEVIN_WORKSPACE_ROOT` (or `workspace_root` kwarg); a run with neither fails `no_workspace` |

## Verification model

- `probe()` runs `devin --version` plus a bounded `initialize` +
  `session/new` handshake (never a prompt) and reports the real distribution
  version. `agentInfo.version` (`0.0.0-dev`) is a build string and is never
  used for compatibility pinning.
- `discover_models()` parses `devin models list --format json` and requires
  **exact** catalog membership of `swe-2-max` — the CLI's fuzzy selector
  matching is not trusted. The catalog's cost tier is checked against
  `DEVIN_EXPECTED_COST_TIER` (default `Free`) as an operator guardrail, not a
  pricing claim. Results are cached for `DEVIN_CATALOG_TTL_SECONDS`
  (default 300 s).
- `execute()` re-checks that cached verification **on every run, before the
  ACP agent is spawned**: the TTL cache is consulted again so a stale `Free`
  listing is never a standing authorization. An absent, not-`Free`, expired
  or unreadable catalog fails the run with `catalog_not_verified` before the
  agent subprocess exists — the check itself only ever spawns the read-only
  `models list` command, never an agent prompt.
- Verification status is honest: a parsed catalog missing the model →
  `failed`; an unreadable/malformed catalog → `unknown`; a matching catalog →
  `passed`. No canary inference is ever run to "verify" a model.
- If `DEVIN_EXPECTED_VERSION` is set, `devin --version` must match it.

## Session & mode safety

- Spawned as `env -u DEVIN_REFUSAL_FALLBACK -u DEVIN_MODEL
  -u DEVIN_PERMISSION_MODE devin --permission-mode accept-edits acp --model
  swe-2-max`. Inherited model/permission/fallback overrides are stripped; no
  hidden model fallback is possible.
- Argv is *not* the safety boundary: even `--permission-mode dangerous` leaves
  the ACP session at `accept-edits`. The driver therefore reads the
  acknowledged `modes.currentModeId` from `session/new` and only trusts a mode
  change after a `session/update` `current_mode_update` notification echoes
  the requested id (`session/set_mode`'s `{}` result is not an
  acknowledgement).
- `bypass` mode is selectable only when the injected
  `RuntimeContext.permissions` policy allows `devin.acp.session_mode.bypass`.
  It is never chosen just because approval would be inconvenient; without an
  explicit policy the run fails with `mode_not_authorized` before any prompt.
- The session's `configOptions` `model` select `currentValue` must equal the
  pinned model before `session/prompt` is sent; a mismatch fails with
  `model_mismatch`.

## Permissions

`session/request_permission` (agent → client) is answered with an explicit
denial: a `reject_*` option when the agent offers one, otherwise the
`cancelled` outcome, and a `permission.required` event is emitted. Same-run
approval continuation is **unsupported** — there is no channel for an
operator to approve a request mid-run in this slice. All other agent→client
methods (`fs/*`, `terminal/*`, private cognition RPCs, …) receive a standard
JSON-RPC `-32601` method-not-found error rather than hanging the turn.

## Event semantics

- `run.started` is emitted after every admission gate (executor, deadline,
  model pin, workspace, catalog re-verification) and immediately **before**
  the ACP agent subprocess is spawned — the first effectful action of the
  run. A denied run emits `run.failed` only; a failed spawn still produces
  `run.started` → `run.failed` rather than silence.
- Only `agent_message_chunk` text becomes `message.delta`. Thought chunks,
  plans, tool logs and stderr are never answer text; tool calls surface as
  `tool.started`/`tool.completed`/`artifact.created` internal events.
- `stopReason: end_turn` means the turn ended — **not** that tests passed.
  `verification` is always `not_run`; the driver never claims verification.
- `stopReason` `max_tokens` / `max_turn_requests` / `refusal` → `partial`
  outcome; unknown stop reasons → `protocol_error`.
- Usage is `provenance: unknown` with null token counts. The ACP
  `usage_update` notification reports context occupancy, not billing; no
  zero-cost or fabricated token values are ever emitted.
- stderr is drained into a bounded tail for diagnostics classification only;
  its content (which may carry secrets) is never surfaced in events.

## Cancellation & failure

- `cancel()` sends `session/cancel`, grants the agent a bounded window
  (`CANCEL_TURN_GRACE_SECONDS`) to close the in-flight turn with a `cancelled`
  stopReason, then terminates the process group via the transport. A cancel
  acknowledgement is never treated as proof of stop — only confirmed
  termination counts.
- EOF mid-turn, malformed/oversized frames, JSON-RPC errors, deadline
  expiry, handshake timeouts and transport failures all terminate the run
  with a terminal event (`run.failed`/`run.cancelled`) and leave the
  workspace exactly as the agent left it — there is no fallback or retry
  after partial side effects.
- Surviving process-group members after confirmed leader exit are reported
  in the cancel detail but deliberately not chased.

## Operator configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `DEVIN_CLI` | `devin` | CLI executable path |
| `DEVIN_MODEL` | `swe-2-max` | Exact model id (must be `swe-2-max`) |
| `DEVIN_EXPECTED_VERSION` | unset | Pin `devin --version` output |
| `DEVIN_WORKSPACE_ROOT` | unset | Static trusted `cwd` fallback; the Runner's bound workspace wins when present |
| `DEVIN_ACP_MODE` | `accept-edits` | Requested session mode (`bypass` needs permission policy) |
| `DEVIN_EXPECTED_COST_TIER` | `Free` | Catalog cost-tier guardrail |
| `DEVIN_CATALOG_TTL_SECONDS` | `300` | Model-catalog cache lifetime |

## Explicitly unsupported in this slice

- Any model other than `swe-2-max`; any generic tier/effort value.
- Session resume / `loadSession` / latest-session reuse.
- Same-run permission approval continuation.
- Token usage, cost, or context-occupancy metering as usage data.
- Private cognition RPCs and any agent→client method other than
  `session/request_permission`.
- Production enablement: no preset routing activates this driver yet.
