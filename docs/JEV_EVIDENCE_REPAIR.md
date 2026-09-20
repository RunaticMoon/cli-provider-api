# JEV evidence/verification repair (S2, S3, S4 + buffer bounds)

Scope: `packages/kanban/src/cli_provider_kanban/worktree.py` and the new
`packages/kanban/src/cli_provider_kanban/_verify_supervisor.py`. Signatures
of `capture_diff`/`changed_files`/`run_verification` are unchanged;
failures raise `WorktreeError` (the state worker catches it in both the
dispatch and resolve call sites). `VerifyResult` gained one optional field
`cleanup` (`confirmed | failed | unsupported`) and `Worktree` an optional
`git_common_dir`.

## What changed

### S2 — broken/moved tree is a hard evidence failure
* `changed_files` and `capture_diff` first call `_revalidate_bound_tree`:
  bound root still a real directory at its canonical path (no relocation,
  no new symlink components), `.git` link present and regular, `rev-parse`
  git-dir resolves under `rev-parse --git-common-dir`, `--show-toplevel`
  resolves back to the bound path, the pinned base commit still exists,
  and `merge-base --is-ancestor base HEAD` holds. HEAD != base is fine —
  the worker may legitimately commit; HEAD must merely *descend from* base.
* Any non-zero git exit on the evidence path is `WorktreeError`, never an
  empty `changed_files`/zero-byte diff (probe 1 now raises instead of
  reporting `[]` while `etc_shadowish.txt` sits on disk).
* Every git call routes through `_git`, which runs under a minimal env
  (`GIT_CONFIG_NOSYSTEM`, system/global config to `/dev/null`, no
  askpass/pager/editor/proxy inheritance) with the unconditional safety
  `-c`s plus an explicit reset of every loaded dangerous key
  (`filter.*`, `diff.*`, `core.fsmonitor`, `core.hooksPath`, `alias.*`,
  `include*`/`includeIf`, credential/gpg/ssh/pager/difftool/mergetool)
  enumerated from `git config --list -z --includes`. Model-written
  `.gitattributes` + repo-config filter/textconv/fsmonitor/hook entries
  therefore cannot execute during evidence; `--no-ext-diff` and
  `--no-textconv` are also passed on the diff itself.
* `Worktree.git_common_dir` recorded at admission pins repository identity
  across receipt reconstruction where the caller supplies it; without it,
  consistency is still proven internally (gitdir under common dir,
  top-level == bound path).

### S3 — complete inventory the run cannot influence
* `changed_files` = `git diff --name-status -z` against a **private**
  `GIT_INDEX_FILE` seeded by `read-tree <base>` and `add -N -f -A`
  (intent-to-add for every real file including ignored/excluded), UNION
  the symmetric difference of a bounded `os.scandir` filesystem walk and
  `git ls-tree -r -z <base>`.
* Consequences: `.gitignore`, `info/exclude`, `core.excludesFile` cannot
  hide output (`-f` forces intent); `assume-unchanged`/`skip-worktree`
  flags live in the shared index, which is never consulted — flagged files
  still show; untracked, dotfiles, newline-bearing and unicode names are
  exact (NUL-separated parsing); symlinks are recorded, never followed;
  the shared index is never mutated (`add -N` hits only the private index —
  no residue, verified by `test_no_shared_index_residue`); a nested repo's
  inner files are still listed as changed even though the diff shows only
  the gitlink (scope view is strictly more complete — the safe direction).
* Bounded enumeration fails closed: walk cap 200k paths, git metadata cap
  64 MiB, config enum cap 4 MiB, unparseable `name-status` stream.
* Exclusions are exactly `.cli-provider-runner.lock` and the documented
  verifier scratch prefix `.jev-verify/`, applied to BOTH the scope and
  diff views (pathspec excludes) — the lock can no longer leak into the
  persisted diff while being hidden from scope. No dist/log/cache bypasses.
* `validate_prepared_worktree` additionally requires disk inventory ==
  base tree at admission: an ignored file pre-existing in the prepared
  tree is refused (it would be indistinguishable from run output later).

### Buffer safety
* `_run_capture` streams stdout under a byte cap *and* a deadline (select
  loop, stderr drained on a side thread), always reaping the child and
  closing its pipes. Oversize kills the reader-side child and labels the
  diff `\n[diff truncated]\n` — the cap is enforced while reading, never
  by buffering the whole `--binary` output and slicing. Git metadata reads
  fail closed on cap or deadline instead of consuming a truncated stream.

### S4 — supervised verification
* `run_verification` now spawns `_verify_supervisor.py` — a small separate
  helper process that sets `PR_SET_CHILD_SUBREAPER` on itself only (never
  on the dispatcher), launches the leader in a new session with
  `stdin` closed, waits it out under the declared deadline, then on EVERY
  end (success, nonzero, timeout, broken pipes, signal-abort) repeatedly
  scans `/proc` ppid-ancestry and SIGKILLs every process whose chain still
  reaches it — catching same-group children, `setsid` escapees and
  double-forked orphans (subreaper adoption makes orphans reparent to the
  helper, so detection is deterministic, not group-based). No `killpg` is
  ever issued, so a reaped leader's recycled pgid cannot redirect a signal
  at an unrelated process; each signal is preceded by a fresh ancestry
  re-verification of that pid.
* The helper reports `{leader_exit, timed_out, spawn_error, unsupported,
  survivors}` over a dedicated inherited fd (never interleaved with child
  output). `ok=True` requires leader exit 0, no timeout and zero
  survivors; missing report, survivors, a killed helper or an unsupported
  host all yield `ok=False` (`cleanup` = `failed`/`unsupported`).
* Child environment is an explicit minimal allowlist: `PATH`, private
  `HOME`/`TMPDIR` (`mkdtemp` outside the worktree, 0700), `LANG`/`LC_ALL`,
  `PYTHONDONTWRITEBYTECODE=1`, `GIT_CONFIG_NOSYSTEM` and inert git author
  identity. No inherited env — no credentials, `PYTHONPATH`, `LD_*`,
  proxies or session vars. This is environment scrubbing, **not** a
  filesystem or network sandbox.
* Bounded total deadline: leader deadline + 10s reap budget enforced by
  the helper; the parent additionally kills the helper at
  deadline + 25s and fails closed ("supervision incomplete").

## Behavioural evidence (probe parity)

Disposable copies of the parent's probes (imports rewritten, originals
untouched) on this branch:

* probe1 (git fail-open): RED — empty evidence after `.git` removal;
  GREEN — `changed_files`/`capture_diff` raise `WorktreeError` while the
  out-of-scope file remains on disk.
* probe6b (ignored/excluded): RED — `dist/payload.sh`, `harvest.log` and
  `sneaky.txt` invisible; GREEN — all three appear in `changed_files`,
  `check_scope` violations and the persisted diff.
* probe2 (children/env): RED — `ok=True` with a surviving `setsid`
  descendant that later wrote `OUT_OF_SCOPE_late.txt` and the inherited
  `JEV_FAKE_SECRET`; GREEN — `ok=True` in ~0.2s, descendant killed before
  its write, secret absent (scrubbed env).

## Changed expectations vs the previous unsafe behaviour

* `changed_files`/`capture_diff` raise `WorktreeError` where they used to
  return `[]`/`""` on git failure — dispatch/control call sites must catch
  (owned by the parallel state worker).
* `validate_prepared_worktree` now also refuses a prepared tree whose disk
  content differs from the base tree even when `git status` is clean
  (hidden ignored/excluded file) — new test
  `test_prepared_worktree_hidden_ignored_file_refused`.
* `run_verification` no longer leaks the dispatcher env and reaps
  descendants on the success path; hosts without Linux `/proc` +
  `PR_SET_CHILD_SUBREAPER` get `ok=False`/`cleanup="unsupported"` instead
  of running unsupervised.

## Honest limitations

* **Not an OS sandbox.** The run and the dispatcher share a UID: a hostile
  process can still write outside the worktree, signal/ptrace our other
  processes, or hold a killable-in-principle child in uninterruptible
  sleep (a `D`-state survivor is reported as `survivors` → fail closed,
  but cannot be force-reaped). Nothing here claims same-UID containment.
* **TOCTOU.** Verification and evidence capture are sequential bounded
  steps, not an atomic snapshot; a same-UID writer racing the few
  milliseconds between them is outside this scope (cgroup/namespace
  isolation is the durable fix and is explicitly out of scope).
* **PID-reuse window.** Ancestry is re-verified immediately before each
  signal; a same-UID adversary could theoretically win a pid-reuse race in
  that microsecond window. Fail-closed survivor reporting bounds the
  consequence to evidence rejection, not a wrong signal target.
* **Admission strictness.** Prepared trees must contain exactly the base
  tree on disk (operator-managed exceptions: Runner lock,
  `.jev-verify/`). Trees with legitimately pre-seeded ignored content need
  operator cleanup; this is intentional fail-closed behaviour.
* **Filter neutralization is global to evidence git.** Legitimate
  repos relying on clean/smudge filters or textconv for real content will
  see unconverted content in the evidence diff; the safer reading is
  deliberate since the config declaring them is run-writable.
* **Ephemeral dev lane.** `prepare_worktree`/`checkout -B` also run under
  the scrubbed env — hooks/filters in the shared config are bypassed there
  too (correct for untrusted config; noted for LFS-style setups).
* **Diff coverage vs inventory.** Embedded repos (nested `.git`) appear in
  the diff only as a gitlink; their inner files are still scope-visible via
  the filesystem walk. Submodule trees checked out inside a prepared
  worktree are treated as run output and will be flagged — operator
  worktrees are expected clean at base.
* **Helper channel.** The report fd is small and bounded; a corrupted or
  absent report fails closed. A helper that must be killed by the parent
  leaves its subtree's adoption chain to init — that path reports
  `cleanup="failed"`, never "confirmed".

## Test evidence

* `packages/kanban/tests/test_evidence_review.py` — 16 tests:
  revalidation (5), inventory (8), bounded diff (2), plus one admission
  fixture in `test_admission.py`.
* `packages/kanban/tests/test_verification_review.py` — 14 tests: normal /
  nonzero / timeout / capped output / closed stdin; same-group child on
  success and timeout; setsid double-fork delayed write; pipe-holding
  child; env scrubbing (+ positive control), private HOME, unsupported-host
  refusal; stale-identity spy + live-descendant positive control.
* `uv run --all-packages pytest packages/kanban -q -o addopts=''
  -W error::pytest.PytestUnraisableExceptionWarning` — 407 passed.
