# Dispatcher state/contract repair (bounded)

Base: `6bf7d235ce5d269ee80eb03317e6a0820b29aa5d` on `fix/jev-review-state`.
Scope: `dispatch.py`, `control.py`, `hermes_bridge.py`, new `evidence.py`,
and their tests. `worktree.py`, the verifier, `compiler.py`,
`wrapper_client.py`, `policy.py`, `conftest.py`, and the existing docs are
untouched — the worktree/verifier defects (probes 1, 2, 6b) belong to the
parallel workstream.

## Contract binding (S1)

`control resolve` now holds the run to the **reserved** contract before
any verification or board effect:

- the spec re-resolved from the card must hash to `res.spec_hash`;
- the loaded policy must fingerprint to `res.policy_fingerprint`;
- the wrapper run view must echo the reserved context — `task_id`,
  `workspace_id`, `attempt_id`, and the `metadata.execution` tuple
  (`task_revision`, `base_revision`, `route`, `policy_version`).

Drift refuses with `ControlError`; the receipt stays `unknown` and no
fresh checks are ever labelled with an old fingerprint.

## Ownership fencing (S1/L4/L2)

The bridge's installed connections are autocommit (`isolation_level=None`)
and `write_txn` begins `BEGIN IMMEDIATE` itself and refuses nesting — a
serial sequence of Python calls on one bridge connection is NOT a
transaction against other connections, and no public mutator is wrapped
in an outer transaction. Fencing is therefore done by `_GuardedConn`, a
small connection proxy handed to the EXISTING kernel mutator: on the
mutator's real `BEGIN IMMEDIATE`/`BEGIN EXCLUSIVE` the proxy first lets
the SQLite write lock be acquired, then re-validates the caller's
read-only ownership predicates under that lock; a failed predicate rolls
the fresh transaction back and returns a typed refusal before any write.
Deferred `BEGIN`s, top-level `SAVEPOINT`s, `executescript`, and bare
autocommit writes are refused outright, so a kernel transaction-shape
change fails closed instead of silently skipping the guard. Canonical
Hermes APIs still own all SQL, events, hooks, and commits; there are no
raw status writes.

- `get_task` now reports `latest_run_id`, `review_run_id`, and
  `last_block` (kind/reason/run_id) — read-only provenance.
- Bridge op `block_owned`: blocks only while the card's live
  `current_run_id` equals the caller's run (`block_task`'s
  `expected_run_id` CAS, plus the lock-held guard that the card is still
  `running`/`ready` with no foreign run live). A card claimed by another
  process after the caller's read is refused untouched — the foreign run
  is never ended.
- Bridge op `reopen_review_if`: reopens a review only when the latest
  `review_requested` event carries the caller's run, re-validated under
  the write lock — a second connection that reopened/reclaimed/requested
  a newer review in between wins; the stale call refuses and never
  demotes it.
- Bridge op `unblock_owned`: unblocks only while, under the write lock,
  the card is still `blocked`/`scheduled`, has no foreign live run, no
  run newer than `latest_run_id`, and its latest block event still
  carries the caller's run and the anchored dispatch marker
  (`resolve <id>:` prefix or `[dispatch <id>]` suffix — never a floating
  substring). `claim` accepts an optional `expected_run_id` fence
  (resolve uses it): under the lock the card must hold no run newer than
  the caller's and no foreign live run.
- The unfenced `block`/`unblock`/`reopen_review` ops remain as operator
  escape hatches; dispatch/control no longer call them — every internal
  board mutation goes through the fenced ops, and every dispatch-filed
  block reason carries the `[dispatch <id>]` marker.
- Resolve's review handoff: a card blocked on a run this dispatch did
  not own, or by a block lacking this dispatch's anchored marker, is
  never auto-unblocked; a card owned by a newer kernel run (or an
  unprovable one) is an explicit `held` manual-review outcome. A
  `ready`/`todo` card is re-claimed under `jev-resolve:<id>` through the
  fenced claim, and the NEW kernel run id is written back to the receipt
  so every later control refers to the real handoff run.
- `cmd_approve` unblocks only through `unblock_owned` fenced to the
  `needs_approval (<id>):` marker it filed — a later foreign block or a
  live run is never silently cleared.
- `cmd_cancel` refuses terminal receipts (`aborted`/`blocked`/`failed`/
  `cancelled`) outright — they mutate nothing and signal nothing.
- `_recover_stale` and `_cancelled_path` use the same fences: a stale
  receipt can never end a foreign run or demote a newer review. A
  validated review wins over a late cancel.

## HTTP refusal safety (L1)

An HTTP error without an authoritative run view proves nothing about
execution. 400/401/403/404/422/429/500/502/504 and malformed bodies now
leave the receipt `unknown` (permanently blocking re-dispatch) and park
the card — no re-submit after a plain unblock, no internal retries. An
error body carrying a typed run view still reconciles through the normal
run-status path.

## Lane/preset provenance (L3 + effort)

- Evidence and review metadata record `lane`, `classified_route`,
  `submitted_model`, `server_preset` (`"unreported"` when absent),
  `route_binding` (`classified`/`unclassified`), `synthetic`, and effort
  (`hint`, `runner_pin`, `observed: "unknown"` — observed is honestly
  unknown, never inferred).
- A direct preset is `classified` only when it is a concrete candidate
  of the classified route. Admission now enforces this BEFORE submit: a
  bound-but-noncandidate preset that is not the declared mock fixture
  (`mock/` namespace — the development tracer) is refused before claim
  and before the wire; the post-execution `synthetic` bit is not
  approval for an unrelated native preset. The mock fixture still
  submits and completes to review only when the authoritative run view
  self-reports `synthetic`; otherwise the handoff is refused as an
  unverifiable binding (`quality_failed`, card blocked) and its evidence
  is labelled `route_binding: unclassified`.
- A preset outside the runner binding's `allowed_presets` still refuses
  at admission — no wire.

## Evidence persistence (S5)

New `evidence.py` — one `persist_diff` helper for both dispatch and
resolve:

- diffs are sanitized (known credential values + generic secret shapes)
  BEFORE write; the recorded digest covers the persisted sanitized
  bytes; `sanitized`/`redaction` labels say exactly what was done (not
  full DLP);
- `evidence/` is created `0700`, files `0600` at `os.open` — nothing
  relies on umask or a post-hoc chmod of a world-readable file;
- writes are verify-then-truncate: the parent dir is pinned by FD
  (`O_DIRECTORY`+`O_NOFOLLOW`, re-`fstat`ed — an ancestor swap cannot
  redirect the leaf open), the leaf is opened `O_NOFOLLOW` WITHOUT
  `O_TRUNC`, and `fstat` on the actual descriptor must show a regular,
  euid-owned, single-linked file at the same `(st_dev, st_ino)` the
  pre-open `lstat` inspected before `ftruncate`+write touch it — a
  hardlink swap at open time refuses and leaves the shared sentinel
  inode untouched.

## Inspection fail-closed (S2 catch-side)

`run_verification`/`collect_artifacts`/`changed_files`/`check_scope`/
`capture_diff` are wrapped in `dispatch` AND `resolve`: any
`WorktreeError`/`OSError` becomes a `quality_failed` non-replay handoff
(card blocked, receipt `blocked`) — never a successful review, never
swallowed uncertainty. (The `worktree.py` raising itself is the parallel
workstream.)

## Tests

`tests/test_state_repair.py` (26 tests) reproduces each class on a real
temp Hermes board + real loopback stub: contract drift refusal, foreign
block hold, newer-run hold, the 9-status HTTP matrix + typed run view,
terminal-cancel refusal, stale-cancel/recovery fencing, preset binding,
provenance recording, injection-driven inspection failure in both paths,
and evidence perms/sanitization/sentinels. RED at base: 23/26 fail for
the right reasons; the 3 passes are preserved-behavior checks.

`tests/test_parent_state_fences.py` (supplied parent regression, kept)
and `tests/test_state_fences.py` run deterministic two-connection
interleavings in a subprocess against the real installed kernel on a
temp board — the second connection's mutation is injected inside the
wrapped mutator, between the bridge's pre-read and its `BEGIN
IMMEDIATE`, no sleeps: stale `block_owned` never ends the foreign run,
stale `reopen_review_if` never demotes the newer review, fenced
`unblock_owned`/`claim` refuse a reclaimed or re-held card and a card a
newer run used, with true-owner positive controls for every fenced op.
Evidence tests inject the leaf hardlink/inode swap and the ancestor-dir
swap at `os.open` — the sentinel is never truncated — plus the L3
pre-submit refusal for a bound noncandidate native preset and the
mock-fixture lane that still reaches the wire.

Remaining limitation: the fenced ops guard the caller-validated
predicate only — an op invoked without fence args (e.g. the operator
`unblock` escape hatch) is intentionally unfenced; the guards cover
ownership/read-only predicates at `BEGIN IMMEDIATE`, not arbitrary
post-commit invariants.
