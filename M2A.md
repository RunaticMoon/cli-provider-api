# M2A — reusable stdio process transport + Antigravity driver (fixtures only)

> **Historical.** This documents the original M2A slice. The Antigravity driver
> has since been updated to the current official nested protocol
> (`init`/`step_update`/`result` payloads nested under the event name), exact
> `agy models` catalog + pinned-version verification, request-bound model
> selection, and init-level model/permission/cwd checks before the prompt. See
> `docs/NATIVE_PROTOCOLS.md` for the current contract.

This slice adds the first native driver package. It is **fixture-tested only**:
no test starts the real `agy` binary, contacts an account, or performs
inference, and **no preset is enabled**. Nothing here is evidence that
Antigravity integration is complete.

## Added

```
packages/transports   cli-provider-transports   + LocalProcessExecutor, NdjsonProcessTransport,
                                                SpawnedProcess, terminate_process_group
drivers/antigravity   cli-driver-antigravity    AntigravityDriver (entry point cli_provider.drivers)
```

### Bounded stdio process transport

- Explicit `argv` list, never a shell; the executable and arguments come from
  operator configuration, never from a run request.
- The child gets its own process group. **Every** signal (the group `SIGTERM`
  `SIGTERM` as well as the `SIGKILL` escalation) is gated on the leader being
  alive as far as that check can tell, so a merely late reap notification does
  not by itself signal a reusable pgid (a best-effort guard, not a pidfd-level
  guarantee). On
  hosts without `/proc` that liveness check cannot be made and the previous
  signalling behaviour applies unchanged. A descendant that ignores `SIGTERM` and
  outlives a promptly-exiting leader is deliberately not chased; it is
  **reported** through `surviving_group_members()` — in the `run.cancelled`
  reason on both the cancel and the deadline path, in the cancel detail, and in
  the Runner's deadline-cancelled detail (bounded to 200 characters) — rather than
  being claimed as cleaned up. The Runner carries and truncates that detail, and
  both the carry-through and the truncation are pinned by UDS runner tests. `group_members()` excludes zombies, so "still alive" means alive.
- `aclose()` releases every pipe through the subprocess transport, so cleanup
  does not depend on a descendant closing its inherited write ends.
- Protocol frames on stdout, diagnostics on stderr, strictly separate. Only a
  bounded stderr tail is retained, and callers get a safe classification rather
  than the raw text (which may contain secrets).
- Frames, total output and stderr are all bounded; malformed JSON is a protocol
  error and is never repaired or skipped.
- `terminate()`/`aclose()` report whether termination was **confirmed**.
  `aclose()` also releases the pipes and lets asyncio's deferred pipe callbacks
  run, so no subprocess transport outlives the event loop.

### Antigravity driver

- Manifest: `driver_id=antigravity`, explicit SDK version, stdio transport,
  `synthetic=false`.
- Invocation: `agy --input-format stream-json --output-format stream-json` with
  no `-p`, plus `--model <exact backend id>` only when the operator pinned one.
  The public preset alias is never passed to the CLI.
- Envelope: discriminated by `event`; the real first frame is
  `{"event": "init", "conversation_id": ..., "init": {...}}` (verified by a
  read-only handshake, no prompt). Frames outside the documented set
  (`init`/`step_update`/`result`) are refused, and the documented order is
  enforced: a `step_update` or `result` before `init` is a protocol error.
- Precedence: a `result` frame that has already been received wins over a cancel
  requested at the same moment (the CLI really did finish the turn); the Runner's
  late-cancel handling keeps that validated terminal. A cancel is only turned into
  `run.cancelled` when it stopped the CLI before a result arrived.
- Only `agent_response.text_delta` becomes `message.delta`. Planning, checkpoint
  and progress content never becomes answer text. Tool steps map to
  `tool.started`/`tool.completed`; a denied tool keeps the outcome `partial`
  because a soft-denied tool can coexist with a zero exit code.
- EOF without a `result` frame, a malformed/oversized frame, or an undocumented
  event is a failed run - never a successful completion. Usage is reported
  `unknown`, never fabricated, and verification stays `not_run`.
- Cancellation and deadline both terminate the process group; a terminal
  `run.cancelled` is emitted only when termination is confirmed, otherwise the
  run ends without a terminal event so the Runner records `unknown`.
- Capabilities are deliberately conservative: `workspace_write`, `vision`,
  `web_search` and `structured_output` are `false`/`none` until an operator
  canary or a later slice actually implements and exercises them.
- Cancellation and deadline win over every abnormal end that follows them.
  A stop we caused (clean EOF, a frame truncated mid-write by the kill, a prompt
  write that fails because the CLI died, or an expired deadline) is reported
  `run.cancelled` **only when termination is confirmed**; an unconfirmed stop ends
  without a terminal event so the Runner records `unknown`. `missing_result` and
  `protocol_error` are reserved for a stream that ended badly with no cancel or
  expired deadline in force. The prompt write is bounded by the same finite
  deadline, so a CLI that never reads stdin cannot block past it.

## Verification

```bash
uv run pytest packages/transports drivers/antigravity
# same, with leaked-subprocess warnings treated as failures
uv run pytest packages/transports drivers/antigravity \
  -W error::pytest.PytestUnraisableExceptionWarning
```

49 focused tests (19 transport, 30 driver) pass, including
`test_repeated_runs_do_not_leak_descriptors`, which runs 10 consecutive driver
executions with the garbage collector disabled and asserts the descriptor count
grows by at most one incidental descriptor (a leaked transport would show more).

## Explicit limitations

- The real CLI's per-step payload schema (tool/planning kinds, denial marker,
  usage fields) is only partly confirmed: the driver fails closed on anything
  outside the documented envelope, and a native canary is still required before
  any preset is enabled.
- `discover_models` returns operator-pinned exact ids with `verification=unknown`;
  the authenticated catalog is not read, so a preset cannot become available
  from this slice alone.
- No OS sandbox: process groups and argv allowlisting are not isolation.
- No ACP transport, no Devin driver, no sessions, no patch/workspace collection.
- The Runner does not yet construct a `ProcessExecutor`, so the driver fails
  closed with `no_process_executor` when reached through the Runner; wiring the
  executor and a native canary are the next slice's work.
- Two independent reviews of this slice found defects that are now fixed and
  regression-tested: an undocumented `update.text_delta` fallback that could have
  promoted planning text to answer content; a cancellation whose trailing EOF was
  reported as `missing_result`; and the same mislabelling on a truncated
  mid-frame stream and on a prompt write that failed after the CLI was killed.
  Capabilities are now pinned by assertions so an overstatement cannot return.
  A third review found that the "descendants are still cleaned up" claim was
  broader than the code: escalation is now guarded by a leader-liveness check, and
  a surviving descendant is reported instead of being claimed killed.
- With no workspace service supplied, the CLI inherits the Runner's working
  directory; the Runner is expected to always supply an approved workspace.
