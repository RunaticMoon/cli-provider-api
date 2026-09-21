# Native protocol observations

Verified 2026-09-17 on Linux/aarch64. These are interface observations, not production Runner isolation or successful provider inference. CLI binaries, credentials and raw account transcripts are NOT distributed in this repository.

## Antigravity

- Installed CLI: `1.2.7` (pinned via `AGY_EXPECTED_VERSION`; an earlier read-only observation recorded `1.2.5`).
- Invocation: `agy --input-format stream-json --output-format stream-json --model <exact-id>` — no `-p` in stream-json mode. `--dangerously-skip-permissions` is supported invocation-locally and is emitted only when the operator enables it (`AGY_ALLOW_SKIP_PERMISSIONS`) AND the bound workspace's execution config grants `antigravity.dangerously_skip_permissions`.
- `agy models` prints a `Fetching available models...` preamble then tab-separated `<model-id>\t<label>` rows; there is no JSON mode. Discovery matches whole ids only — no prefix/suffix/fuzzy promotion.
- Input frame: `{"event":"user","message":{"content":"..."}}`.
- Output envelopes carry their payload nested under a key matching the event name: `{"event":"init","init":{...}}`, `{"event":"step_update","step_update":{...}}`, `{"event":"result","result":{...}}`.
- `init` is emitted before any prompt is read and carries `cwd`, `model`, `permission_mode`, `tools`. Observed 2026-09-21: `init.model=gemini-3.8-flash-high`, `init.permission_mode=always-proceed` under `--dangerously-skip-permissions`; the process exited 0 when stdin closed without a task. The driver verifies `init.model`/`permission_mode`/`cwd` against the admitted request and the operator permission decision before sending the prompt; mismatch fails the run before any task text is sent.
- `step_update.step_type` values observed/documented: `user_input`, `checkpoint`, `agent_response`, `tool`, plus planning kinds. Only `step_type == "agent_response"` `text_delta` is answer text — every other step type is consumed and dropped.
- Tool steps carry `tool_name` and `tool_info` (`name`/`parameters`/`output`/`error`). A `tool_info.error` (including soft permission denials) marks the tool event failed and downgrades the run outcome to `partial` — it can still coexist with `result.status == "SUCCESS"` and process exit 0.
- `result.status`: `SUCCESS` is the only status that completes a run. `ERROR`, `CANCELED`, `INTERRUPTED`, `INVALID`, `WAITING`, `RUNNING`, a missing/malformed status, or a stream that ends without `result` all fail or cancel the run — never complete it.
- `result.usage` (`input_tokens`/`output_tokens`/...) is cumulative in persistent sessions; this driver is stateless (one process per run), so a valid non-zero observation is reported as first-turn usage and absent/zero usage is `unknown` — never a fabricated zero.
- Catalog membership is not evidence of inference, quota or billing: `agy models` proves the account lists the id only.

Sources: https://antigravity.google/docs/cli/headless/ (live page fetched by parent 2026-09) and the parent's no-inference handshake observation (`init` before prompt, exit 0).

## Devin

- Installed CLI: `3000.10.31 (b98cc431)`.
- `devin acp --help` confirms JSON-RPC over stdio. `--model` accepts fuzzy selectors; this project must validate an exact discovered backend ID before passing it, rather than depend on fuzzy selection.
- Actual `devin acp` accepted a newline-delimited JSON-RPC `initialize` with `protocolVersion:1`, empty client capabilities and a client name/version. It returned protocol version 1 and agent capabilities. **No session prompt/inference performed.**
- Reported `agentInfo.version` was `0.0.0-dev`, not the distribution version above. Do not substitute one for the other when pinning compatibility.
- Advertised capabilities included loadSession and image/embedded-context prompts. This is NOT permission to expose those features in the MVP: preset/API policy still restricts them.
- Help exposes optional refusal fallback and `DEVIN_REFUSAL_FALLBACK`. A driver must not configure hidden model fallback or inherit it accidentally. Provider/preset selection is external to the driver.
- ACP optional methods must be negotiated, not assumed; use explicit sessions only if later enabled. No implicit latest-session reuse.

Sources:
- https://docs.devin.ai/cli/acp/xcode
- https://agentclientprotocol.com/protocol/initialization
- installed `devin acp --help` and the actual read-only initialization handshake.

## Evidence limits

The initial native handshakes did not send user tasks, run tools, test billing/quota, test native cancellation, or establish separate-OS-user confinement. Such checks remain distinct from mock Driver/UDS/API tests. They must be completed before advertising native presets as executable.
