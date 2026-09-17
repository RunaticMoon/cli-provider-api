# cli-provider-api — accepted requirements

This is a new public project, not a claim that the SDK/config syntax already exists. Build a common runtime for official CLI agents. New drivers must not require changing API routes, RunController, SSE rendering, or 9Router integration. Lead/final fallback stays outside this runtime; 9Router owns tier/model candidate selection. Core and drivers never perform provider/model fallback.

## Boundaries
- Distinguish Driver (CLI protocol), Transport (stdio/ACP/PTY), Runner instance (host/auth environment), Model preset (public alias with verified backend binding).
- Driver has manifest, async probe/discover_models, execute→AsyncIterator[RunEvent], cancel→CancelResult, aclose. RuntimeContext supplies process executor, workspace, permissions, cancellation/deadline, redacted logger, session store. Dependency injection is NOT an OS sandbox.
- Official machine-readable paths first: Antigravity stream-json stdin/stdout (no -p prompt with that mode); Devin `devin acp` JSON-RPC. Verify installed versions/schemas. PTY only explicit compatibility mode selected BEFORE execution, never automatic rerun.
- Trusted separately packaged drivers discovered by `cli_provider.drivers` entry points. Only Runner imports allowlisted installed distributions/versions; API process never imports a driver. Requests cannot select package/import/executable/cwd/env/MCP command.
- Capabilities combine driver, verified CLI version/model and preset policy. Declare streaming native/buffered/none; sessions none/explicit_resume/persistent; external_tool_calls/internal_tools; roles native/serialized/unsupported; structured_output; vision; workspace_write; web_search; usage provenance.
- MVP text messages only. Reject unsupported tools/tool_choice, images and unimplemented sampling parameters; never silently drop them. Internal CLI tool logs are NOT OpenAI tool_calls. Serialize roles/content faithfully and label this as serialized, not native roles.

## Public API
- GET /v1/models, POST /v1/chat/completions.
- Same methods under /providers/{allowlisted-driver}/v1, scoped to that driver; no API code per driver.
- GET /api/v1/runs/{run_id}, GET .../events, POST .../cancel, GET /api/v1/artifacts/{artifact_id}.
- /health/live, /health/ready; no fake /v1/responses.
- Authenticate every model/run/artifact route. API-key principal bounds presets, workspace IDs and concurrency; ownership checked on every lookup. Bounded body/event/output/queue/runtime sizes.
- OpenAI sync or SSE responses for bounded jobs. 202+run_id is NOT a Chat Completions result. Stream answer deltas only; no internal plans/tool logs. Buffered drivers identified as such. Keepalives do not extend deadline; do not concatenate another provider after stream failure.

## Lifecycle and isolation
- SQLite single API instance first; no Redis/Celery/Kubernetes.
- task_id is logical work, attempt_id is one provider invocation. Atomically reserve before execution, one active/unknown attempt per caller+task; changed task content under an ID conflicts. Stable idempotency cannot be assumed through 9Router: test it.
- queued→starting→running→completed/failed/cancelling→cancelled; abnormal Runner disconnect/driver termination means unknown, not empty success. Do not auto-rerun unknown/effectful attempts. Cancellation requested and termination confirmed are separate facts.
- status and outcome are separate. Exit 0 does not prove tools/tests ran. Verification reports passed/failed/not_run/unknown with source and reason. Usage is reported/estimated/unknown, never invented zero.
- Stateless fresh CLI session per request initially. Optional resume binds caller+instance+preset+workspace+explicit session ID; never --continue/latest. Stateful append and stateless replay are distinct.
- Review: read-only snapshot; test review uses separate disposable writable copy. Code: isolated writable copy of approved source/base revision, return patch/artifacts/evidence, never mutate original or push/merge. Search: empty workspace, explicit web policy, citations/time. Worktrees are not an OS security boundary.
- API and each Runner use separate OS identities/auth stores for production. Unix sockets initially; private-network mTLS remote Runner is later extension. No Docker socket/other HOME/Lead credentials. Official CLI owns login/refresh; don't extract/reimplement OAuth. Auth stores may require legitimate writes. Trusted drivers and CLI share a trust domain. Exercise actual OS file/network/process isolation before claiming it.

## Registry/config
- Operator config owns runner endpoints/allowed driver packages/versions/presets/backend IDs/task policies; no executable selection in HTTP.
- Enabled alone is insufficient: auth/version/model/capability probes must pass. Empty required env is config error, not default-model selection.
- Drivers never know free/easy/standard/hard/max. Worker may map task_type+tier→Combo; actual candidate order/model/effort policy stays with 9Router. Do not mix review/code/search fallback pools indiscriminately.
- Fusion alias is not a backend ID; disabled until exact noninteractive lead/effort/sidekick pairing is verified. No guessed model IDs.

## Milestones and acceptance
1. SDK + Mock Driver + real API/Runner boundary: success/failure/timeout/cancel/idempotency/ownership/unsupported-capability tests. Must actually run, not scaffolding only.
2. Antigravity NDJSON: official live protocol, auth/model validation, errors/permission denial/process cleanup. Fixtures don't prove account execution.
3. Devin ACP: initialization, sessions, prompt/update/permissions/cancel; one native task, optional capability negotiation.
4. Actual pinned 9Router integration in isolated DB/auth: slash aliases, provider scopes, SSE/errors/deadline and task/idempotency preservation; pre-execution fallback first. Do not modify the existing production gateway.
5. Workspace/patch/evidence and real OS isolation: original unchanged, no residual processes, filesystem/network negative controls.
6. Fusion, explicit sessions, extra transports/drivers enabled only after capability/canary tests. Don't advertise unimplemented features.

Pin CLI/driver versions; update only after protocol fixtures + real canary. Publish no credentials, runtime data, personal home paths or real account transcripts. Public source does not imply public network deployment. Record implemented, fixture-tested, native-tested and blocked/not-run separately.
