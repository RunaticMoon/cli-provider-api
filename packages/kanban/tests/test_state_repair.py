"""State/contract repair — regression tests for the parent dispatch verdict.

Reproduces the reported defect classes on a REAL temp Hermes board + real
loopback stub wrapper (never mocks for board truth):

- S1/L4: ``control resolve`` must hold the run to the RESERVED contract
  (spec_hash + policy_fingerprint + run echo) and fence every board
  mutation to the receipt's own kernel run — never auto-unblock a foreign
  block, never promote a card a newer run owns.
- L1: an HTTP refusal without an authoritative run view is UNKNOWN, never
  proof of pre-execution rejection — no re-submit after a plain unblock.
- L2: a stale/terminal receipt's cancel/recovery must not mutate the
  board a newer dispatch owns nor signal a run it does not have.
- L3: a direct lane preset must be a concrete candidate of the classified
  route; submitted preset/lane and observed-vs-declared effort are
  recorded honestly.
- S5: persisted evidence is sanitized BEFORE write, digested as stored,
  and created 0700/0600 with no-follow semantics.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from cli_provider_kanban.control import (
    ControlError,
    cmd_cancel,
    cmd_resolve,
)
from cli_provider_kanban.dispatch import dispatch_once
from cli_provider_kanban.evidence import EvidenceError, persist_diff
from cli_provider_kanban.kernel import KernelBridge
from cli_provider_kanban.store import DispatchStore
from cli_provider_kanban.worktree import WorktreeError

from conftest import (  # noqa: F401
    CURRENT_OS_USER,
    dispatch_policy,
    requires_hermes,
    stub_wrapper,
    write_policy,
)
from test_control_hardening import (
    _card_with_spec,
    _crashed_receipt,
    _stub_run_for,
)
from test_dispatch import HERMES_ENV_KEYS, _spec_for

pytestmark = requires_hermes


@pytest.fixture
def env(tmp_path, monkeypatch, dispatch_policy, stub_wrapper):
    for key in HERMES_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    data, rev = dispatch_policy
    data["execution"]["base_url"] = stub_wrapper.base_url
    db = tmp_path / "kanban.db"
    bridge = KernelBridge(
        db, env_extra={"HERMES_HOME": str(tmp_path / "hermes_home")}
    )
    store_path = tmp_path / "dispatch.db"
    yield {
        "db": db, "bridge": bridge, "store_path": store_path,
        "policy_data": data, "rev": rev, "stub": stub_wrapper,
        "policy_path": None,
    }
    bridge.close()


def _policy_path(env, tmp_path):
    if env["policy_path"] is None:
        env["policy_path"] = write_policy(tmp_path, env["policy_data"])
    return env["policy_path"]


def _tick(env, tmp_path):
    return dispatch_once(board_db=env["db"],
                         policy_path=_policy_path(env, tmp_path),
                         store_path=env["store_path"])


def _card(env, tmp_path, **spec_over):
    """Ready card + task_map spec; returns task_id."""
    return _card_with_spec(env, tmp_path) if not spec_over else _card_spec(
        env, tmp_path, spec_over)


def _card_spec(env, tmp_path, spec_over):
    data, rev = env["policy_data"], env["rev"]
    tid = env["bridge"].call(
        "create_task", title="c", assignee="jev-native", body="x",
    )["task_id"]
    spec = _spec_for(tid, rev, **spec_over)
    del spec["task_id"]
    tm = tmp_path / f"tm_{tid}.json"
    tm.write_text(json.dumps({tid: spec}))
    env["policy_data"]["task_map"] = str(tm)
    env["policy_path"] = write_policy(tmp_path, env["policy_data"])
    return tid


def _rewrite_spec(env, tmp_path, tid, **spec_over):
    """Rewrite the card's spec channel (untrusted) to a weaker contract."""
    spec = _spec_for(tid, env["rev"], **spec_over)
    del spec["task_id"]
    tm = tmp_path / f"tm_{tid}.json"
    tm.write_text(json.dumps({tid: spec}))
    env["policy_data"]["task_map"] = str(tm)
    env["policy_path"] = write_policy(tmp_path, env["policy_data"])


# -- S1: resolve holds the run to the RESERVED contract -----------------------

def test_resolve_refuses_card_spec_drift(env, tmp_path):
    """Card-body/task-map edit after an unknown run must NOT swap the
    verification contract — drift refuses, the receipt stays unknown."""
    tid = _card(env, tmp_path)
    run = _stub_run_for(env, tid, env["rev"],
                        env["policy_data"]["policy_version"])
    prepared = env["policy_data"]["workspaces"]["ws-main"]["prepared_worktree"]
    res = _crashed_receipt(env, tmp_path, tid=tid, run_id=run.run_id,
                           worktree=prepared)
    # Untrusted channel now offers a weaker contract (scope + argv swapped).
    _rewrite_spec(env, tmp_path, tid, allowed_scope=["evil.txt"],
                  verification={"argv": ["true"], "criteria": "exit 0"})
    with pytest.raises(ControlError, match="contract drift"):
        cmd_resolve(store_path=env["store_path"], board_db=env["db"],
                    policy_path=_policy_path(env, tmp_path),
                    dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    store = DispatchStore(env["store_path"])
    after = store.get(res.dispatch_id)
    store.close()
    assert after.state == "unknown"
    assert after.evidence is None  # no fresh checks under an old label


def test_resolve_refuses_policy_fingerprint_drift(env, tmp_path):
    tid = _card(env, tmp_path)
    run = _stub_run_for(env, tid, env["rev"],
                        env["policy_data"]["policy_version"])
    prepared = env["policy_data"]["workspaces"]["ws-main"]["prepared_worktree"]
    res = _crashed_receipt(env, tmp_path, tid=tid, run_id=run.run_id,
                           worktree=prepared)
    # Policy edited after the reservation — fingerprint no longer matches.
    # (The run's execution echo must track the new policy_version so the
    # ONLY difference under test is the fingerprint.)
    env["policy_data"]["policy_version"] = "2099-01-01.9"
    env["policy_path"] = write_policy(tmp_path, env["policy_data"])
    for r in env["stub"].runs.values():
        r["execution"]["policy_version"] = "2099-01-01.9"
    with pytest.raises(ControlError, match="drift"):
        cmd_resolve(store_path=env["store_path"], board_db=env["db"],
                    policy_path=env["policy_path"],
                    dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    store = DispatchStore(env["store_path"])
    assert store.get(res.dispatch_id).state == "unknown"
    store.close()


def test_resolve_full_fenced_path_reviews(env, tmp_path):
    """The legitimate resolve still works: in-flight -> unknown -> the
    run settles completed+succeeded -> resolve verifies and hands off to
    review under a provable fence."""
    env["stub"].run_status = "unknown"  # run never settles on tick 1
    tid = _card(env, tmp_path)
    report = _tick(env, tmp_path)
    out = next(r for r in report["results"] if r["task_id"] == tid)
    assert out["action"] == "in_flight", out
    _tick(env, tmp_path)  # recovery marks the receipt unknown
    store = DispatchStore(env["store_path"])
    res = store.for_task(tid)[-1]
    store.close()
    assert res.state == "unknown"
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        == "blocked"
    for run in env["stub"].runs.values():
        run["status"] = "completed"
        run["outcome"] = "succeeded"
    out = cmd_resolve(store_path=env["store_path"], board_db=env["db"],
                      policy_path=_policy_path(env, tmp_path),
                      dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    assert out["resolved"] == "review", out
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        == "review"


def test_resolve_never_unblocks_foreign_operator_block(env, tmp_path):
    """L4: a card blocked by an unrelated operator block is an explicit
    manual-review hold — resolve must not auto-clear it."""
    tid = _card(env, tmp_path)
    run = _stub_run_for(env, tid, env["rev"],
                        env["policy_data"]["policy_version"])
    prepared = env["policy_data"]["workspaces"]["ws-main"]["prepared_worktree"]
    res = _crashed_receipt(env, tmp_path, tid=tid, run_id=run.run_id,
                           worktree=prepared)
    # An operator (not this dispatch) blocks the card — ends our run but
    # carries no dispatch attribution.
    env["bridge"].call("block", task_id=tid, kind="needs_input",
                       reason="operator freeze — unrelated to dispatch")
    out = cmd_resolve(store_path=env["store_path"], board_db=env["db"],
                      policy_path=_policy_path(env, tmp_path),
                      dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    assert out["resolved"] == "held", out
    task = env["bridge"].call("get_task", task_id=tid)["task"]
    assert task["status"] == "blocked"  # foreign block left in place
    store = DispatchStore(env["store_path"])
    assert store.get(res.dispatch_id).state == "unknown"
    store.close()


def test_resolve_held_when_newer_run_owns_card(env, tmp_path):
    """A newer kernel run on the card means the stale run's resolve cannot
    promote it — explicit hold, card untouched."""
    tid = _card(env, tmp_path)
    run = _stub_run_for(env, tid, env["rev"],
                        env["policy_data"]["policy_version"])
    prepared = env["policy_data"]["workspaces"]["ws-main"]["prepared_worktree"]
    res = _crashed_receipt(env, tmp_path, tid=tid, run_id=run.run_id,
                           worktree=prepared)
    # A different actor claims the card — a NEWER run row now exists.
    env["bridge"].call("block", task_id=tid, kind="needs_input",
                       reason=f"resolve test block [dispatch {res.dispatch_id}]")
    env["bridge"].call("unblock", task_id=tid)
    claim2 = env["bridge"].call("claim", task_id=tid, claimer="jev:other")
    assert claim2["claimed"]
    out = cmd_resolve(store_path=env["store_path"], board_db=env["db"],
                      policy_path=_policy_path(env, tmp_path),
                      dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    assert out["resolved"] == "held", out
    task = env["bridge"].call("get_task", task_id=tid)["task"]
    assert task["status"] == "running"
    assert task["current_run_id"] == claim2["task"]["current_run_id"]
    store = DispatchStore(env["store_path"])
    assert store.get(res.dispatch_id).state == "unknown"
    store.close()


# -- L1: no-run-view HTTP refusals leave an UNKNOWN blocking receipt ---------

@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 500, 502, 504])
def test_http_refusal_without_run_view_blocks_redispatch(
    env, tmp_path, status
):
    """ANY refusal without an authoritative run view may have forwarded —
    the receipt goes unknown (permanently blocking) and a plain unblock
    can never cause a re-submit."""
    tid = _card(env, tmp_path)
    env["stub"].submit_status_override = (
        status, {"error": {"message": "upstream failure"}})
    report = _tick(env, tmp_path)
    out = next(r for r in report["results"] if r["task_id"] == tid)
    assert out["action"] == "unknown", out
    store = DispatchStore(env["store_path"])
    rec = store.for_task(tid)[-1]
    store.close()
    assert rec.state == "unknown"
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        == "blocked"
    # Operator unblocks — the stated remedy for a "rejected request" — and
    # the wrapper is healthy again; there must still be NO re-submit.
    env["bridge"].call("unblock", task_id=tid)
    env["stub"].submit_status_override = None
    report2 = _tick(env, tmp_path)
    assert not any(r["task_id"] == tid and r.get("dispatched")
                   for r in report2["results"])
    submits = [r for r in env["stub"].requests
               if r["path"] == "/v1/chat/completions"]
    assert len(submits) == 1


def test_error_with_typed_run_view_reconciles(env, tmp_path):
    """An authoritative terminal run view on an error IS reconciled — the
    run's own truth applies, not the HTTP status."""
    tid = _card(env, tmp_path)
    env["stub"].submit_status_override = (
        500, {"run": {
            "run_id": "run_err1", "task_id": tid, "attempt_id": "att_e1",
            "status": "failed", "outcome": "provider_error",
            "workspace_id": "ws-alpha", "preset": "devin/swe-2-max",
            "execution": {
                "task_revision": "1",
                "base_revision": env["rev"],
                "route": "worker.code.standard",
                "policy_version": env["policy_data"]["policy_version"],
            },
        }})
    report = _tick(env, tmp_path)
    out = next(r for r in report["results"] if r["task_id"] == tid)
    assert out["action"] == "failed", out
    store = DispatchStore(env["store_path"])
    assert store.for_task(tid)[-1].state == "failed"
    store.close()


# -- L2: stale receipts never mutate a newer dispatch's board -----------------

def test_terminal_receipt_cancel_refused(env, tmp_path):
    """Probe-4 class: an aborted receipt's cancel must not demote the card
    a NEWER dispatch handed to review, and must not signal any run."""
    tid = _card(env, tmp_path)
    store = DispatchStore(env["store_path"])
    d1 = store.reserve(task_id=tid, task_revision="1", spec_hash="sha256:x",
                       policy_fingerprint="sha256:p", workspace_id="ws-main",
                       base_revision=env["rev"], route="worker.code.standard")
    store.transition(d1.dispatch_id, "aborted", detail="claim lost")
    store.close()
    report = _tick(env, tmp_path)  # real dispatch -> verified -> review
    out = next(r for r in report["results"] if r["task_id"] == tid)
    assert out["action"] == "review", out
    with pytest.raises(ControlError, match="terminal"):
        cmd_cancel(store_path=env["store_path"], board_db=env["db"],
                   policy_path=_policy_path(env, tmp_path),
                   dispatch_id=d1.dispatch_id, actor=CURRENT_OS_USER)
    task = env["bridge"].call("get_task", task_id=tid)["task"]
    assert task["status"] == "review"  # never demoted
    assert not [r for r in env["stub"].requests if r["path"].endswith("/cancel")]


def test_stale_receipt_cancel_never_ends_foreign_run(env, tmp_path):
    """A stale receipt may persist its intent and signal its OWN run — but
    the board mutation is fenced: a live run it does not own is never
    ended by its cancel."""
    tid = _card(env, tmp_path)
    claim = env["bridge"].call("claim", task_id=tid, claimer="jev:other",
                               ttl_seconds=600)
    foreign_run = claim["task"]["current_run_id"]
    store = DispatchStore(env["store_path"])
    d1 = store.reserve(task_id=tid, task_revision="1", spec_hash="sha256:x",
                       policy_fingerprint="sha256:p", workspace_id="ws-main",
                       base_revision=env["rev"], route="worker.code.standard")
    store.transition(d1.dispatch_id, "claimed", kernel_run_id=999)
    store.transition(d1.dispatch_id, "submitted", run_id="run_stale")
    store.close()
    out = cmd_cancel(store_path=env["store_path"], board_db=env["db"],
                     policy_path=_policy_path(env, tmp_path),
                     dispatch_id=d1.dispatch_id, actor=CURRENT_OS_USER)
    assert out["cancel_requested"] is True
    assert out["wrapper"]["status"] == "not_found"  # honest 404 reporting
    task = env["bridge"].call("get_task", task_id=tid)["task"]
    assert task["status"] == "running"
    assert task["current_run_id"] == foreign_run  # never ended
    store = DispatchStore(env["store_path"])
    assert store.get(d1.dispatch_id).cancel_requested
    store.close()


def test_recover_stale_never_blocks_foreign_run(env, tmp_path):
    """Crash recovery must not end a kernel run it does not own."""
    tid = _card(env, tmp_path)
    claim = env["bridge"].call("claim", task_id=tid, claimer="jev:other",
                               ttl_seconds=600)
    foreign_run = claim["task"]["current_run_id"]
    store = DispatchStore(env["store_path"])
    res = store.reserve(task_id=tid, task_revision="1", spec_hash="sha256:x",
                        policy_fingerprint="sha256:p", workspace_id="ws-main",
                        base_revision=env["rev"], route="worker.code.standard")
    store.transition(res.dispatch_id, "claimed", kernel_run_id=999)
    store.transition(res.dispatch_id, "submitted", run_id="run_ghost")
    store.close()
    report = _tick(env, tmp_path)
    assert any(r["dispatch_id"] == res.dispatch_id
               and r["state"] == "unknown" for r in report["recovered"])
    task = env["bridge"].call("get_task", task_id=tid)["task"]
    assert task["status"] == "running"
    assert task["current_run_id"] == foreign_run  # never ended


# -- L3: direct lane preset must bind the classified route --------------------

def test_direct_preset_outside_binding_never_wires(env, tmp_path):
    """A direct preset the operator binding does not allow refuses before
    any wire — no provenance claim the binding can't back."""
    env["policy_data"]["execution"]["model"] = "unbound/preset"
    env["policy_path"] = write_policy(tmp_path, env["policy_data"])
    tid = _card(env, tmp_path)
    report = _tick(env, tmp_path)
    out = next(r for r in report["results"] if r["task_id"] == tid)
    assert out["action"] == "blocked", out
    assert "does not allow required presets" in out["reason"]
    assert not [r for r in env["stub"].requests
                if r["path"] == "/v1/chat/completions"]


def test_noncandidate_preset_needs_declared_synthetic_lane(env, tmp_path):
    """A bound-but-noncandidate preset is the declared mock exception: it
    may only complete as the synthetic lane. A run that does NOT self-
    report synthetic is an unverifiable binding — blocked, never a review
    carrying classified route provenance it cannot support."""
    env["policy_data"]["execution"]["model"] = "mock/text"  # bound, non-candidate
    env["policy_path"] = write_policy(tmp_path, env["policy_data"])
    tid = _card(env, tmp_path)
    report = _tick(env, tmp_path)
    out = next(r for r in report["results"] if r["task_id"] == tid)
    assert out["action"] == "blocked", out
    assert "unverifiable binding" in out["reason"]
    store = DispatchStore(env["store_path"])
    rec = store.for_task(tid)[-1]
    store.close()
    prov = rec.evidence["provenance"]
    assert prov["route_binding"] == "unclassified"
    assert prov["submitted_model"] == "mock/text"
    assert prov["synthetic"] is False
    task = env["bridge"].call("get_task", task_id=tid)["task"]
    assert task["status"] == "blocked"  # never promoted to review


def test_provenance_recorded_on_receipt(env, tmp_path):
    """The submitted preset/lane and declared-vs-observed effort are on the
    receipt — the route label alone is not provenance."""
    tid = _card(env, tmp_path)
    report = _tick(env, tmp_path)
    out = next(r for r in report["results"] if r["task_id"] == tid)
    assert out["action"] == "review", out
    store = DispatchStore(env["store_path"])
    rec = store.for_task(tid)[-1]
    store.close()
    prov = rec.evidence["provenance"]
    assert prov["lane"] == "direct"
    assert prov["classified_route"] == "worker.code.standard"
    assert prov["submitted_model"] == "devin/swe-2-max"
    assert prov["server_preset"] == "devin/swe-2-max"  # stub echoes it
    assert prov["route_binding"] == "classified"
    assert prov["synthetic"] is False
    assert prov["effort"]["observed"] == "unknown"


# -- S2-class: inspection failure fails closed in BOTH paths ------------------

def test_dispatch_inspection_failure_quality_failed(
    env, tmp_path, monkeypatch
):
    """changed_files raising WorktreeError (broken git view) is a hard
    quality_failed — never a clean bill, never a review."""
    def _boom(wt):
        raise WorktreeError("git view broken by the run")

    monkeypatch.setattr(
        "cli_provider_kanban.dispatch.changed_files", _boom)
    tid = _card(env, tmp_path)
    report = _tick(env, tmp_path)
    out = next(r for r in report["results"] if r["task_id"] == tid)
    assert out["action"] == "blocked", out
    assert "evidence inspection failed" in out["reason"]
    store = DispatchStore(env["store_path"])
    assert store.for_task(tid)[-1].state == "blocked"
    store.close()
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        == "blocked"


def test_resolve_inspection_failure_quality_failed(
    env, tmp_path, monkeypatch
):
    def _boom(wt):
        raise OSError("evidence capture I/O failure")

    monkeypatch.setattr(
        "cli_provider_kanban.control.changed_files", _boom)
    tid = _card(env, tmp_path)
    run = _stub_run_for(env, tid, env["rev"],
                        env["policy_data"]["policy_version"])
    prepared = env["policy_data"]["workspaces"]["ws-main"]["prepared_worktree"]
    res = _crashed_receipt(env, tmp_path, tid=tid, run_id=run.run_id,
                           worktree=prepared)
    out = cmd_resolve(store_path=env["store_path"], board_db=env["db"],
                      policy_path=_policy_path(env, tmp_path),
                      dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    assert out["resolved"] == "blocked", out
    assert "inspection failed" in out["reason"]
    store = DispatchStore(env["store_path"])
    assert store.get(res.dispatch_id).state == "blocked"
    store.close()


# -- S5: evidence persistence -------------------------------------------------

def test_evidence_dir_and_file_permissions(env, tmp_path):
    """Permissive umask must not leak: dir is 0700 and the diff file 0600
    AT CREATION — no post-chmod of a world-readable artifact."""
    old = os.umask(0o000)
    try:
        tid = _card(env, tmp_path)
        report = _tick(env, tmp_path)
    finally:
        os.umask(old)
    out = next(r for r in report["results"] if r["task_id"] == tid)
    assert out["action"] == "review", out
    store = DispatchStore(env["store_path"])
    rec = store.for_task(tid)[-1]
    store.close()
    diff = rec.evidence["diff"]
    dpath = Path(diff["path"])
    assert stat.S_IMODE(os.stat(dpath.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(dpath).st_mode) == 0o600
    assert diff["sanitized"] is True
    assert diff["sha256"] == hashlib.sha256(
        dpath.read_bytes()).hexdigest()


def test_persist_diff_sanitizes_known_secrets(tmp_path):
    store_path = tmp_path / "dispatch.db"
    store_path.touch()
    info = persist_diff(store_path, "d_sec1",
                        "token=SECRET-VALUE-9\n+ content\n",
                        secrets=["SECRET-VALUE-9"])
    raw = Path(info["path"]).read_text()
    assert "SECRET-VALUE-9" not in raw
    assert "[REDACTED]" in raw
    # Digest refers to the persisted (sanitized) bytes.
    assert info["sha256"] == hashlib.sha256(
        Path(info["path"]).read_bytes()).hexdigest()


def test_persist_diff_refuses_symlink_and_hardlink_sentinels(tmp_path):
    store_path = tmp_path / "dispatch.db"
    store_path.touch()
    # Establish the dir with a first write.
    persist_diff(store_path, "d_seed", "ok\n")
    evdir = tmp_path / "evidence"
    target = tmp_path / "sentinel.txt"
    target.write_text("DO NOT TOUCH\n")
    # Symlink sentinel: never followed, never written through.
    link = evdir / "d_link.diff"
    link.symlink_to(target)
    with pytest.raises(EvidenceError):
        persist_diff(store_path, "d_link", "malicious\n")
    assert link.is_symlink()
    assert target.read_text() == "DO NOT TOUCH\n"
    # Hardlink sentinel: multi-linked targets are refused, content intact.
    hard = evdir / "d_hard.diff"
    os.link(target, hard)
    with pytest.raises(EvidenceError):
        persist_diff(store_path, "d_hard", "malicious\n")
    assert target.read_text() == "DO NOT TOUCH\n"
    assert os.stat(hard).st_nlink == 2


def test_diff_secret_redaction_end_to_end(env, tmp_path):
    """A credential the run left in the worktree lands REDACTED in the
    persisted diff — the stored bytes, not just the report."""
    import sys as _sys

    data = env["policy_data"]
    data["execution"]["credential_file"] = str(tmp_path / "cred")
    (tmp_path / "cred").write_text("SECRET-TOKEN-zzz\n")
    os.chmod(tmp_path / "cred", 0o600)
    env["policy_path"] = write_policy(tmp_path, data)
    # The trusted verification argv writes the run's "output" — a file
    # containing the credential — inside the bound worktree.
    cred = tmp_path / "cred"
    tid = _card_spec(env, tmp_path, {
        "verification": {
            "argv": [
                _sys.executable, "-c",
                f"import pathlib; pathlib.Path('leak.txt').write_text("
                f"pathlib.Path({str(cred)!r}).read_text())",
            ],
            "criteria": "exit 0",
        },
        "artifacts": [],
    })
    report = _tick(env, tmp_path)
    out = next(r for r in report["results"] if r["task_id"] == tid)
    # Out-of-scope leak -> quality_failed, but the diff is still persisted.
    assert out["action"] == "blocked", out
    store = DispatchStore(env["store_path"])
    rec = store.for_task(tid)[-1]
    store.close()
    diff_raw = Path(rec.evidence["diff"]["path"]).read_text()
    assert "SECRET-TOKEN-zzz" not in diff_raw
    assert "[REDACTED]" in diff_raw
    assert "leak.txt" in diff_raw  # the evidence is still meaningful
