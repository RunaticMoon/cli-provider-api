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

Every board mutation is fenced to the run that produced it:

- `get_task` now reports `latest_run_id`, `review_run_id`, and
  `last_block` (kind/reason/run_id) — read-only provenance.
- New bridge op `block_owned`: blocks only while the card's live
  `current_run_id` equals the caller's run (CAS via `block_task`'s
  `expected_run_id`) or no run is live; a card running under a different
  run is refused untouched.
- New bridge op `reopen_review_if`: reopens a review only when the
  latest `review_requested` event carries the caller's run. Both ops run
  serially on the bridge's single connection — one request is the narrow
  transaction.
- All `block`/`reopen_review` sites in `dispatch.py`/`control.py`
  (recovery, cancel, quality failure, review handoff) now carry
  `[dispatch <id>]` attribution and the receipt's `kernel_run_id` fence.
- Resolve's review handoff: a card blocked by a block this dispatch did
  not file is never auto-unblocked; a card owned by a newer kernel run
  (or an unprovable one) is an explicit `held` manual-review outcome. A
  `ready` card is re-claimed under `jev-resolve:<id>` so the handoff
  rides a run this resolve owns.
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
  of the classified route. An operator-bound preset outside the route is
  the declared synthetic dev lane: it completes to review only when the
  authoritative run view self-reports `synthetic`; otherwise the handoff
  is refused as an unverifiable binding (`quality_failed`, card blocked).
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
- writes are `O_NOFOLLOW` + `fstat`-verified: planted symlinks and
  multi-linked targets are refused and left untouched; replaced files
  must be regular, euid-owned, single-linked; ancestor dirs must be real
  owned directories.

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
