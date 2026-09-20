# Kanban → Jev shadow classifier (Slice 1)

`packages/kanban` (`cli-provider-kanban`) is the first bounded slice of the
Hermes Kanban → Jev → 9Router integration: a **card contract**, a
**rules-first classifier**, and a **read-only shadow CLI**. It is not an
orchestrator: it never writes to the board, never claims or dispatches cards,
and never starts a runtime. No core/API/Runner code is touched.

## Scope and scope safety

- Cards enter scope by the explicit assignee `jev-native` (policy
  `scope.assignee`). The id is validated at policy load: it must not be a
  reserved name and must not collide with an existing Hermes profile under
  `HERMES_HOME`/`~/.hermes`, so the stock dispatcher can never spawn a scoped
  card (`skipped_nonspawnable` is the observed stock behaviour).
- `scope.statuses` selects card statuses (default `ready`, `todo`).
- The board is opened with SQLite `mode=ro` only. `hermes_cli.kanban_db.connect`
  is deliberately NOT used — it would create a missing DB and run schema
  migrations. A missing file is an error; no `.init.lock`/`.dispatch.lock`/
  `-wal`/`-shm` artefacts are created. Byte-stability is tested.

## Card contract (`TaskSpec`)

Required fields (all validated, `extra="forbid"`): `task_id`,
`task_revision`, `role` (worker/planner/reviewer/researcher), `capability`,
`tier`, `effort_hint`, `objective`, `inputs`, `dependency_ids`,
`relevant_files`, `allowed_scope`, `artifacts`, `verification`
(`argv` list + `criteria`), `acceptance_criteria`, `prohibited`,
`base_revision`, `workspace_id`, `risk_flags`. Optional: `replan_count`,
`decomposition` (`depth`/`children`).

Where the contract comes from, in order:

1. the card body — whole-body JSON, or exactly one fenced
   ```` ```jev-task-spec ```` block; or
2. the local Lead-maintained `task_map` file (policy `task_map` path, relative
   to the policy file) keyed by task id.

## Rules-first classification (`classify`)

No LLM exists in this slice (`classifier.llm` only accepts `"disabled"`).
Order of evaluation:

1. Untrusted overrides → `replan`: any `tasks.model_override` /
   `provider_override` / `reasoning_effort` / preset `workspace_path` on the
   row, or unknown spec keys (executor/model/workspace fields are forbidden by
   the schema — operator-owned, never card-chosen).
2. Missing/invalid spec, task-id mismatch, unknown capability, untrusted
   `workspace_id`, `dependency_ids` that drifted from the board's actual
   parents, or decomposition beyond `max_depth`/`max_children` → `replan`.
3. `replan_count > replan_cap`, any declared `risk_flags`
   (authn/authz/security/billing/destruction/migration/production/
   external_effects), or a gated tier (`hard`/`max`) → `needs_approval`.
   Risk/approval is never overridden by `confidence` — it is advisory only and
   policy-configurable (`classifier.confidence.body`/`task_map`/`no_spec`).
4. Route `role.capability.tier` absent from the policy → `replan`; present but
   with no enabled+capability-mapped candidate → `hold`; otherwise `execute`
   with the ordered available candidates.

The five tiers (`free/easy/standard/hard/max`) are enforced separately from
native effort hints (`auto/economy/balanced/thorough/maximum`) — a hint is
never a tier and is never mapped onto a provider effort flag.

`JevDecision`: `task_id`, `task_revision`, `role`, `capability`, `tier`,
`effort_hint`, `route`, `recommended_action`
(`execute`/`replan`/`needs_approval`/`hold`), `risk_flags`, `confidence`
(advisory), `reason`, `policy_version`, `candidates` (ordered *available*
logical candidates; the 9Router compiler owns combo generation — no fallback
in the classifier).

## Caching

Decisions are cached in a JSON file keyed by
`(task_id, task_revision, policy_version)` together with the card fingerprint
(sha256 over spec-bearing columns). A cache hit serves the stored decision;
the same key with a different fingerprint is a `conflict` record — `replan`,
never served — until the Lead bumps `task_revision`.

## Commands

```bash
uv run cli-provider-kanban --help
uv run cli-provider-kanban schema [--out schemas.json]
uv run cli-provider-kanban shadow \
  --board-db /path/to/kanban.db \
  --policy packages/kanban/examples/jev-routing-policy.yaml \
  --out shadow-report.json [--cache shadow-cache.json]
```

Exit 2 + stderr message on missing board, invalid policy, corrupt cache or an
over-limit scope. `python -m cli_provider_kanban` works identically.

## Sample policy

`packages/kanban/examples/jev-routing-policy.yaml` — the only enabled
candidate is `devin-swe-2-max` (`devin` kind, verified `code`/`review`
mapping, cost_tier Free). `bai-code` is present but `enabled: false`
(historical direct-API policy not permitted; no binary on ARM64) and
`devin-opus-review` is disabled (reviewer fallback inactive pending policy
resolution → `reviewer.review.standard` classifies `hold`). Codex is refused
at policy load — kind, id or model.

## Tests

```bash
uv run pytest packages/kanban            # 128 tests
uv run --all-packages pytest             # 443 total, whole workspace
```

`tests/test_hermes_integration.py` drives the **installed** Hermes venv
(`HERMES_AGENT_DIR`/`HERMES_PYTHON` overridable) in a scratch
`HERMES_HOME`/`HERMES_KANBAN_DB` under `tmp_path`: real `create_task`,
read-only shadow on the real schema, and a real `dispatch_once` proving stock
dispatch skips `jev-native` (`skipped_nonspawnable`, no spawn, card
unchanged). Skipped when the venv is absent. Nothing writes the live Hermes
tree, config or profiles.

## Slice status — honest

- **Implemented/tested:** contract schema, spec extraction (body + task_map),
  rules classifier, revision+policy cache with mutation conflict, read-only
  shadow + report, CLI (`--help`/`schema`/`shadow`), sample central policy.
- **Not in this slice (later phases):** card claiming/dispatch, any Runner/API
  wiring, 9Router combo compilation, approvals plumbing, real Devin/BAI
  execution. `recommended_action` is logical only — nothing executes it.
- **Known limits:** read-only `mode=ro` cannot open a WAL-mode board whose
  `-shm` was deleted while `-wal` is still needed for recovery (rare; the
  shadow fails with an error, never escalates to read-write). A preset
  `workspace_path` on an in-scope card is treated as an untrusted override —
  including project-board worktree paths set by `create_task`; jev-native
  cards are expected to use `workspace_id` mapping instead.
