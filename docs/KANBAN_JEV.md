# Kanban → Jev → 9Router dispatch path

`packages/kanban` (`cli-provider-kanban`) is a bounded vertical slice of the
Hermes Kanban → Jev → 9Router integration:

> real Hermes `ready` card → Jev classifier (route only) → thin one-shot
> dispatcher → operator-bound workspace admission → wrapper
> `/v1/chat/completions` (direct preset or compiled 9Router combo) →
> existing wrapper run → trusted verification → Kanban **review**
> (never `done`).

There is no second runtime, scheduler, agent framework, or UI. The board is
the installed Hermes kernel's own `kanban.db`; scheduling stays in Hermes;
runs stay in the existing wrapper; this package adds only a **receipt
sidecar** (`dispatch.db`), a **rules classifier**, a **one-shot dispatch
tick**, **control operations**, and a **policy compiler** for 9Router.

## Architecture

```
kanban.db (Hermes kernel, canonical task state)
   │  list ready / claim / heartbeat / review / block / reopen_review
   ▼  via hermes_bridge.py — a JSON-lines subprocess run under the
cli-provider-kanban     installed Hermes interpreter (no in-process import,
   │                    no raw status writes)
   ├─ dispatch.db (sidecar receipts + approvals + evidence; SQLite, ours)
   ├─ operator-prepared worktree (pinned branch at base_revision) +
   │  protected Runner execution config binding the submitted workspace id
   │  to exactly that path
   └─ WrapperClient → existing wrapper endpoints only:
        POST /v1/chat/completions   (data plane; stream:false;
                                     metadata.task_id, workspace_id,
                                     execution)
        GET  /api/v1/runs/{id}      (control plane — control_base_url)
        POST /api/v1/runs/{id}/cancel
        GET  /api/v1/artifacts/{id}
```

Canonicality: Kanban owns task state, the wrapper owns run state, the
sidecar owns only dispatch receipts/approvals/evidence — it never
re-decides card status. Execution mode is `direct` (a fixed
operator-declared preset alias) or `gateway` (the compiled combo
`jev.<route>`; a static `execution.model` is rejected there so
classification can never be bypassed). Gateway mode requires
`execution.control_base_url` — run truth and cancel never ride the
data-plane combo path.

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
   effect; `needs_approval` writes a durable approval record bound to the
   exact scope (task revision + spec hash + policy fingerprint +
   operation + expiry), blocks the card `needs_input`, and optionally
   registers a Hermes notify sub. An **applied** grant for the identical
   scope is consumed atomically with the reservation — once, never a loop.
2. Effort mapping resolved per-backend (`auto`→null on devin; api backends
   need an explicit `effort_map` entry) — unsupported → block `needs_input`
   *before* claim. A non-null resolved effort additionally requires the
   workspace's `runner_effort_pin` to match — the pinned Runner cannot be
   proven to carry an effort it wasn't configured for, so dispatch refuses
   rather than silently running the wrong effort.
3. **Reserve** the receipt in `dispatch.db` BEFORE any HTTP/worktree
   effect — one active-or-unknown reservation per card across restarts.
4. Dependency proof: every parent must be `done` **and** carry an
   `integrated_revision` that is `git merge-base --is-ancestor` of the
   pinned `base_revision`. Undone parents → skip; missing/non-ancestor →
   block `dependency`.
5. **Workspace admission** — before claim and before any HTTP:
   `workspace.prepared_worktree` must be a real git worktree of the
   configured repo, `HEAD` exactly `base_revision`, clean, on its pinned
   branch; and `workspace.runner_execution_config` must be a trusted file
   (canonical path, owned, 0600/0640, private 0700 parent — the same bar
   the Runner's own loader enforces) whose `workspaces.<submitted id>.root`
   equals the prepared path and whose `allowed_presets` permits the
   submitted model. No binding → fail closed; no wire. The autogenerated
   `jev/<dispatch_id>` worktree lane survives only behind the explicit
   development-only `allow_ephemeral_worktree` flag (results are labeled
   `ephemeral`/`synthetic`).
6. `max_retries` forced to 1 (stock reclaim must never replay an external
   write), then `claim_task` (atomic ready→running, dep-gated). A cancel
   intent already persisted wins here — before any submit.
7. Submit **exactly one** `POST /v1/chat/completions` (`stream:false`,
   `metadata.task_id`/`workspace_id`, and `metadata.execution =
   {task_revision, base_revision, route, policy_version}` when
   `dispatch.send_execution_metadata`). Transport failure after submit →
   receipt `unknown`, card blocked — **never retried**.
8. The claim is renewed for real for the whole HTTP+verification critical
   section — a daemon heartbeat thread calls the kernel `heartbeat` op on
   `dispatch.heartbeat_seconds` (validated `< claim_ttl_seconds`).
9. Canonical run view handling — only `status == "completed"` **and**
   `outcome == "succeeded"` **and** the echoed `task_id`/`workspace_id`/
   `execution` equal to the reserved context may enter verification. A
   foreign-context run is `unknown` (anomalous, never verified). A live or
   unrecognized status holds `in_flight` — receipt stays `submitted`, card
   parked `needs_input`, no review, no blind replay. `cancelled`/`failed`/
   non-`succeeded` → the matching typed terminal, never review.
10. A persisted cancel intent is re-checked atomically before verification
    work (`completing` under `require_no_cancel`) and again at the review
    transition — a late cancel wins over the handoff; the card is pulled
    back (`reopen_review` → blocked `needs_input`) rather than left
    promotable.
11. Verification is real: the card's `verification.argv` must match the
    operator's `verification.commands` full-argv allowlist (exact args; a
    trailing `*` is the only wildcard; the executable head must be in
    `verification.executables`). Runs in the prepared worktree with no
    shell, a process-group kill on timeout, and bounded output capture.
    Post-run evidence covers committed **and** dirty/untracked changes; any
    changed path outside `allowed_scope` is `quality_failed`. Artifacts are
    hashed (sha256, bounded); the diff and sanitized verification output
    are stored durably on the receipt (`evidence/`, known credential values
    redacted) — a `blocked`/`unknown` receipt keeps its evidence too.
12. `request_review` with `expected_run_id` fence; the receipt→`review`
    transition is the same atomic cancel-guarded write. Refused → `unknown`
    — never guessed, never replayed.

Crash recovery at the top of every tick: a `reserved` receipt never reached
the kernel → `aborted`; anything later → `unknown`, card blocked
`needs_input`, manual `resolve` required.

## Control (`control <op>`)

`--actor` is **not** authentication by itself: the actor must be in
`policy.control.operators` **and** equal `pwd.getpwuid(os.geteuid())`
(the invoking OS user), or map to the current euid via the optional
`control.operator_uids` table. Local CLI auth only — not Telegram.

- `cancel --dispatch-id` — persists `cancel_requested` on the receipt
  *first*, then cancels the wrapper run through the control target. The
  wrapper result is reported truthfully (`confirmed` only when the wrapper
  confirms; `not_found`/`unreachable`/`error` are never claimed as
  success). A card in `review` is reopened and blocked — a cancelled
  execution must not stay promotable or silently re-dispatch.
- `approve`/`deny --approval-id` — atomic consume-once; expiry marks the
  record `expired`; wrong actor or replay is refused. An applied grant is
  consumed exactly once, inside the reservation transaction, for the exact
  task-revision/spec-hash/policy-fingerprint scope — a changed card or
  policy re-gates.
- `accept` — **disabled**: a review-state card plus a caller-supplied
  40-hex string is not proof of an integrated, verified revision. The Lead
  verifies the review handoff and completes via Hermes directly. The CLI
  flag is preserved but fails closed.
- `resolve --dispatch-id` — reconciles an `unknown` receipt against the
  wrapper: no run id, a 404, or an unreachable control plane all leave it
  `unknown` (absence is not proof nothing executed — external operator
  investigation); `cancelled`/`failed`/non-`succeeded` mirror the terminal
  truth; `completed`+`succeeded` re-runs the **full** verification contract
  (bound worktree intact, allowlisted argv, artifacts, scope, diff) before
  any review handoff — never a shortcut.

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
    --store dispatch.db --actor <os-user> \
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

**Operator prerequisite:** a workspace bound for real dispatch needs a
prepared worktree pinned at the card's base revision plus a Runner
execution config whose binding points at exactly that path — and the
Runner must be (re)started with `--execution-config` on the verified bytes;
the dispatcher validates the file it was given, not a live Runner's memory.

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
block through the bridge), real git worktrees and ancestry, the kernel
singleton lock excluding a second dispatcher, a real loopback HTTP stub for
the wrapper contract, and — `tests/test_vertical_tracer.py` — the full
vertical over real subprocesses: real `WrapperClient` → real
`cli-provider-api` → real `cli-provider-runner` (mock driver) → run truth,
with the prepared-worktree binding enforced by a real execution config.
The mock lane is asserted `synthetic` in the run view rather than hidden;
the native canary is the parent's post-merge step. The live-9Router apply
path is covered against a stub management API; a real 0.5.81 router smoke
stays opt-in for the parent.

## Honest status

- **Implemented/tested:** route-only contract + classifier with floor
  gates and confidence replan; receipt sidecar with crash recovery, durable
  evidence, and consume-once scoped approval grants; kernel bridge;
  prepared-worktree admission + protected Runner binding validation;
  single-submit dispatch with strict run-status/outcome/context contract;
  real claim heartbeats through the critical section; trusted bounded
  verification with scope enforcement; fenced review handoff; OS-bound
  control actors; cancel/approve/deny/resolve; `accept` disabled
  fail-closed; 9Router plan compile + gated apply; CLI for all of it.
- **Explicitly not here:** no scheduling loop (Hermes cron/timer owns
  cadence), no `done` from the dispatcher or from `accept` (the Lead
  completes via Hermes), no Codex, no fifth backend, no card-chosen
  executor/model/workspace, no retry of a submitted request, no
  plaintext/chat approval auth, no production gateway targets.
- **Known limitations:** in-tick cancel reconciliation uses the data-plane
  client when no `control_base_url` is configured (direct mode is
  same-plane by definition; gateway mode always configures one); the
  Runner's loaded binding is attested by file validation — restart
  freshness is an operator prerequisite, not a cryptographic attestation.
