# 9Router compatibility — preliminary relay probe

Pinned version tested: **9Router 0.5.75**, 2026-09-17.

This probe used a real isolated 9Router process with fresh temporary HOME/state and a synthetic HTTP upstream. It did **not** modify an installed service, connect a real CLI account, or establish complete cli-provider-api integration.

## Observed in both non-stream and SSE modes

- Custom OpenAI-compatible base path `/providers/mock/v1` produced `/providers/mock/v1/chat/completions` correctly.
- A gateway model named `cpamock/mock/text` reached the upstream as exact alias `mock/text`.
- Body `metadata.task_id` and `metadata.workspace_id` survived.
- `system`, `assistant`, `user` message roles survived in order.
- Custom request headers `X-Task-Id` and `Idempotency-Key` did **not** reach the upstream.
- Standard response `id` and the tested `cli_provider` JSON extension survived.
- Custom response header `X-Run-Id` did **not** reach the client.
- SSE `[DONE]` survived. No additional `stream_options` appeared in the observed upstream requests.

Consequently the initial API contract requires IDs in **body metadata**, not only custom headers. Management clients must not depend solely on an upstream run header surviving 9Router.

## Usage

The synthetic upstream omitted usage. The non-stream response still omitted usage. In the streaming case 9Router added a usage chunk marked `estimated:true`. This is gateway estimation, **not CLI-reported usage**. Authoritative CLI provenance belongs to the normalized run result/management endpoint; never relabel estimates as provider-reported counts or add them twice.

## Actual API integration

Two opt-in tests in `tests/integration_9router` subsequently passed with the real
API and UDS Mock Runner behind the isolated pinned gateway. The actual `run`
JSON extension survived non-streaming responses and the final SSE chunk. They
also exercised a pre-execution authentication failure followed by a valid
candidate, owner-scoped status/events/artifact retrieval, cached task replay,
and changed-body conflict.

The real gateway can discard an initial **empty-content metadata-only** chunk.
The API therefore binds the standard completion ID to `chatcmpl-{run_id}` in
live, cached and streaming responses. This gives a management identity even
before the final metadata arrives, without inserting fake answer text. The
integration test checks that the early ID matches the final run and an
authorized management lookup. Send `stream: false` explicitly for non-stream
requests; the pinned gateway's omitted-flag behaviour must not be assumed.

Run the opt-in suite with a trusted local npm app artifact:

```bash
NINEROUTER_APP=/path/to/9router-0.5.75/package/app \
  uv run --all-packages pytest tests/integration_9router -v
```

## Still to verify

Native CLI execution, unknown-state/post-execution fallback across providers,
full timeout/cancellation through the gateway, and OS isolation remain separate
gates. These passes are not evidence of safe coding-job fallback or exactly-once
execution. Native presets are not implemented/enabled in this mock-only alpha.
