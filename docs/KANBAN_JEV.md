# Kanban → Jev → 9Router dispatch path

`packages/kanban` (`cli-provider-kanban`) is a bounded vertical slice of the
Hermes Kanban → Jev → 9Router integration:

> real Hermes `ready` card → Jev classifier (route only) → thin one-shot
> dispatcher → wrapper `/v1/chat/completions` (direct preset or compiled
> 9Router combo) → existing wrapper run → verification → Kanban **review**
> (never `done`).

There is no second runtime, scheduler, agent framework, or UI. The board is
the installed Hermes kernel's own `kanban.db`; scheduling stays in Hermes;
runs stay in the existing wrapper; this package adds only a **receipt
sidecar** (`dispatch.db`), a **rules classifier**, a **one-shot dispatch
tick**, **control operations**, and a **policy compiler** for 9Router.

## Architecture

```
kanban.db (Hermes kernel, canonical task state)
   │  list ready / claim / heartbeat / review / block / complete
   ▼  via hermes_bridge.py — a JSON-lines subprocess run under the
cli-provider-kanban     installed Hermes interpreter (no in-process import,
   │                    no raw status writes)
   ├─ dispatch.db (sidecar receipts + approvals; SQLite, ours)
   ├─ git worktree per card (trusted repo + worktree_root from policy)
   └─ WrapperClient → existing wrapper endpoints only:
        POST /v1/chat/completions   (stream:false; metadata.task_id,
                                     workspace_id, execution)
        GET  /api/v1/runs/{id}
        POST /api/v1/runs/{id}/cancel
        GET  /api/v1/artifacts/{id}
```

Canonicality: Kanban owns task state, the wrapper owns run state, the
sidecar owns only dispatch receipts — it never re-decides card status.

## Card contract (`TaskSpec`)

Required: `task_id`, `task_revision`, `objective`, `inputs`,
`dependency_ids`, `relevant_files`, `allowed_scope`, `artifacts`,
`verification` (`argv` list + `criteria`), `acceptance_criteria`,
`prohibited`, `base_revision` (full 40-hex), `workspace_id`, `risk_flags`.
Optional legacy hints `role`/`capability`/`tier`/`effort_hint`, and a
structured `work` block (`kind` implement/review/research/plan, `design`
ready/draft/unclear, `scope` small/medium/large).

Role/capability/tier derive deterministically from `work` (kind→role+cap,
scope→tier small→easy / medium→standard / large→hard). A `work` block that
conflicts with explicit hints → `replan`; ambiguity → `replan`; never a
heuristic guess. Spec sources: card body (whole JSON or exactly one fenced
`jev-task-spec` block) or the operator `task_map` file.

## Classification (`classify`)

Rules-first; no LLM (`classifier.llm` only accepts `"disabled"`).

1. Untrusted overrides (model/provider/effort/preset workspace on the row,
   unknown spec keys) → `replan`.
2. Missing/invalid spec, id mismatch, unknown capability/workspace,
   dependency drift, decomposition overflow → `replan`.
3. `replan_count` over cap, any known `risk_flags`, gated tier
   (`hard`/`max`), or `unknown` cost → `needs_approval`. These are **floor
   gates** — no confidence value or operator list relaxes them.
4. Confidence below `classifier.min_execute_confidence` → `replan` (a real
   gate, not advisory).
5. Route `role.capability.tier` absent from `policy.routes` → `replan`;
   no enabled+capable candidate → `hold`; else `execute`.

`JevDecision` is **route-only** — no `candidates`. Backend order belongs to
the policy compiler/9Router alone.

## Dispatch (`dispatch --once`)

One bounded tick, serialized by the kernel's `_dispatch_tick_lock` (a second
concurrent dispatcher gets `held=false` and exits). Per card:

1. Classify → `hold`/`replan`/`needs_approval` handled before any side
   effect; `needs_approval` writes a durable approval record, blocks the
   card `needs_input`, and optionally registers a Hermes notify sub.
2. Effort mapping resolved per-backend (`auto`→null on devin; api backends
   need an explicit `effort_map` entry to low/high/max) — unsupported →
   block `needs_input` *before* claim.
3. **Reserve** the receipt in `dispatch.db` BEFORE any HTTP/worktree
   effect — one active-or-unknown reservation per card across restarts.
4. Dependency proof: every parent must be `done` **and** carry an
   `integrated_revision` (in its closing run's metadata) that is
   `git merge-base --is-ancestor` of the pinned `base_revision`. Undone
   parents → skip (stays ready); missing/non-ancestor revision → block
   `dependency`.
5. `max_retries` forced to 1 (stock reclaim must never replay an external
   write), then `claim_task` (atomic ready→running, dep-gated).
6. Fresh worktree at `jev/<dispatch_id>` from `base_revision`; submit
   **exactly one** `POST /v1/chat/completions` (`stream:false`,
   `metadata.task_id`/`workspace_id`, and `metadata.execution =
   {task_revision, base_revision, route, policy_version}` when
   `dispatch.send_execution_metadata` — the agreed core contract shape).
   Duplicate `task_id` → the cached run view is reconciled, never a second
   execution. Transport failure after submit → receipt `unknown`, card
   blocked — **never retried**.
7. Completed run → real verification (bounded `git diff`, declared artifact
   paths confined to the worktree, trusted-argv-only test run) →
   `request_review` with `expected_run_id` fence. Missing artifact or test
   failure → block `needs_input` with a `quality_failed:` reason (Hermes's
   typed kinds are dependency/needs_input/capability/transient) — **never a
   fallback**. Failed/cancelled run → block; the worktree is preserved.
8. Heartbeats renew the claim while the synchronous request is in flight.

Crash recovery happens at the top of every tick: a `reserved` receipt never
reached the kernel → `aborted` (safe to re-dispatch); anything later is
`unknown` → card blocked `needs_input`, manual `resolve` required.

## Control (`control <op>`)

`--actor` must be in `policy.control.operators` — MVP local identity only,
**not** Telegram auth.

- `cancel --dispatch-id` — persists `cancel_requested` on the receipt
  *first*, then cancels the wrapper run; a late completion can never be
  upgraded to review/done afterwards.
- `approve`/`deny --approval-id` — atomic consume-once; expiry marks the
  record `expired`; wrong actor or replay is refused.
- `accept --task-id --integrated-revision` — the ONLY path to `done`:
  requires the card in `review`, a review-state receipt with no pending
  cancel, a live run not cancelled, and a full-commit revision recorded on
  the closing run's metadata.
- `resolve --dispatch-id` — reconciles an `unknown` receipt: completed run
  → review handoff; failed/cancelled → mirror; no run at all → `aborted`
  (the only retryable unknown); still running → report only.

## Compile (`compile`)

`compile --policy P` renders provider-node/provider/combo payloads plus a
wrapper-preset fragment — dry-run by default, secret-free by construction.
Combo `models` order is exactly the policy's `candidates` order. Combos on
mutation tiers (`standard`/`hard`/`max`) are compiled **non-operational**
unless `gateway.assume_core_guard` attests the shared
no-post-dispatch-retry guard is integrated.

`--apply TARGET` is opt-in and refuses anything but a `disposable`
loopback target; credentials come from a JSON file
(`management_token` + `upstream_key`), never argv; combos are read back and
must match exactly or the apply fails.

## Commands

```bash
uv run cli-provider-kanban --help
uv run cli-provider-kanban schema [--out schemas.json]
uv run cli-provider-kanban shadow --board-db kanban.db --policy P --out R.json
uv run cli-provider-kanban dispatch --once --board-db kanban.db \
    --policy P --store dispatch.db
uv run cli-provider-kanban status --board-db kanban.db --policy P \
    --store dispatch.db (--dispatch-id D | --task-id T)
uv run cli-provider-kanban control --board-db kanban.db --policy P \
    --store dispatch.db --actor op-name \
    (cancel --dispatch-id D | approve|deny --approval-id A | \
     accept --task-id T --integrated-revision REV | resolve --dispatch-id D)
uv run cli-provider-kanban compile --policy P [--apply TARGET --credential-file F]
```

## Operations

Activate (all disabled by default; nothing runs unattended):

```bash
# dry-run the compiled plan (secret-free)
uv run cli-provider-kanban compile --policy packages/kanban/examples/jev-routing-policy.yaml
# one manual dispatch tick
uv run cli-provider-kanban dispatch --once \
    --board-db ~/.hermes/kanban/kanban.db --policy <policy> --store ~/.hermes/kanban/dispatch.db
```

The systemd oneshot in `packages/kanban/examples/jev-dispatch.service` is a
**disabled** template — `Type=oneshot`, no `Restart=`, no `[Install]`.
Activation/deactivation commands are in the file's header comment.

Deactivate: stop the unit / stop invoking `dispatch`. Deactivation is
state-preserving — `dispatch.db` and `kanban.db` stay exactly as they are.

Rollback: receipts are durable and never replayed. An `unknown` receipt
means a submission may have happened — resolve it explicitly via
`control resolve`; do not delete `dispatch.db` rows to "retry". Removing the
integration entirely is `git checkout <baseline>` for this package only —
board and wrapper state are untouched.

## Tests

```bash
uv run pytest packages/kanban            # owned suite
uv run --all-packages pytest             # whole workspace
```

Real-thing coverage: Hermes-kernel temp boards (claim/heartbeat/review/
block/complete through the bridge), real git worktrees and ancestry, the
kernel singleton lock excluding a second dispatcher, and a real loopback
HTTP stub for the wrapper contract (submit payload, cached-run reconcile,
cancel, transport→unknown). Mock-only stand-ins are labelled where used
(`FakeWrapper`). The live-9Router apply path is covered against a stub
management API; a real 0.5.81 router smoke stays opt-in for the parent.

## Honest status

- **Implemented/tested:** route-only contract + classifier with floor
  gates and confidence replan; receipt sidecar with crash recovery; kernel
  bridge; trusted worktrees; single-submit dispatch to the existing wrapper
  endpoints; verification→review handoff; approval/cancel/accept/resolve;
  9Router plan compile + gated apply; CLI for all of it.
- **Explicitly not here:** no scheduling loop (Hermes cron/timer owns
  cadence), no `done` from the dispatcher (Lead `accept` only), no Codex,
  no fifth backend, no card-chosen executor/model/workspace, no retry of a
  submitted request, no plaintext/chat approval auth, no production gateway
  targets.
- **Pending the parallel core/driver merge:** `metadata.execution` rides
  only once the core metadata contract lands (`send_execution_metadata`
  flag); native approval continuation awaits a bridge; the shared
  no-post-dispatch-retry guard must be attested before mutation-tier combos
  are operational.
