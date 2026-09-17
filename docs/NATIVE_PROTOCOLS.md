# Native protocol observations

Verified 2026-09-17 on Linux/aarch64. These are interface observations, not production Runner isolation or successful provider inference. CLI binaries, credentials and raw account transcripts are NOT distributed in this repository.

## Antigravity

- Installed CLI: `1.2.5`.
- Installed help advertises `--input-format stream-json`, `--output-format stream-json`, `--model`, `--effort`, `--print-timeout`, `--sandbox`.
- Actual subprocess started with `agy --input-format stream-json --output-format stream-json` (no `-p`) and emitted a valid `init` event before any prompt. Process was then closed. **No inference performed.**
- Official input frame is `{"event":"user","message":{"content":"..."}}`, not a guessed `type:user` envelope. Text-block lists are also documented; non-text blocks must not be silently discarded.
- Output: `init`, `step_update`, one `result` per turn. Only `agent_response.text_delta` is an answer delta; planning/tool/checkpoint content is not automatically user-visible answer text.
- A result's `response` belongs to the current turn; `usage`, `num_turns` and `duration_seconds` in persistent sessions are cumulative. Stateless first version avoids silently summing cumulative usage twice.
- Documented headless soft-denied tools can coexist with process exit 0. Tests/permissions must be reported separately from turn completion.
- Global help does not establish effort support for a specific model. Exact backend models and supported reasoning settings require authenticated validation before preset activation.

Source: https://antigravity.google/docs/cli/headless/ (live page and installed help).

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
