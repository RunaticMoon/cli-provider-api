"""Dispatch tick against the REAL Hermes kernel (temp board) + REAL git
worktrees, with a recording fake wrapper client (mock-only for HTTP).

The board is created by the installed Hermes kernel through the bridge —
cards are real ``hermes_cli.kanban_db`` rows, claims/heartbeats/review are
the real kernel operations. Only the wrapper HTTP hop is faked.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from cli_provider_kanban.dispatch import dispatch_once
from cli_provider_kanban.kernel import KernelBridge
from cli_provider_kanban.store import DispatchStore

from conftest import (
    FakeWrapper,
    dispatch_policy,  # noqa: F401  (fixture)
    hermes_available,
    policy_dict,
    requires_hermes,
    spec_body,
    spec_dict,
    write_policy,
)

pytestmark = requires_hermes

HERMES_ENV_KEYS = ("HERMES_HOME", "HERMES_KANBAN_DB", "HERMES_KANBAN_HOME",
                   "HERMES_KANBAN_BOARD")


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A real Hermes kanban.db + bridge, scoped to tmp_path."""
    for key in HERMES_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    db = tmp_path / "kanban.db"
    bridge = KernelBridge(
        db, env_extra={"HERMES_HOME": str(tmp_path / "hermes_home")}
    )
    yield db, bridge
    bridge.close()


def _make_card(bridge, spec=None, **kw):
    body = spec_body(spec or spec_dict())
    return bridge.call(
        "create_task", title=kw.pop("title", "card"), body=body,
        assignee="jev-native", **kw,
    )["task_id"]


def _spec_for(task_id, base_rev, **over):
    spec = spec_dict(task_id=task_id, base_revision=base_rev,
                     effort_hint="auto",
                     verification={"argv": ["true"], "criteria": "exit 0"},
                     artifacts=[])
    spec.update(over)
    return spec


def _run(board, tmp_path, dispatch_policy, client=None):
    data, rev = dispatch_policy
    policy_path = write_policy(tmp_path, data)
    store = tmp_path / "dispatch.db"
    report = dispatch_once(
        board_db=board, policy_path=policy_path, store_path=store,
        client=client or FakeWrapper(),
    )
    return report, store


def test_task_map_resolves_spec(board, tmp_path, dispatch_policy):
    """Kernel-issued ids can't be known before create, so cards for the
    dispatch tests resolve their spec through the policy task_map."""
    db, bridge = board
    data, rev = dispatch_policy
    tid = bridge.call("create_task", title="mapped card",
                      assignee="jev-native", body="prose only")["task_id"]
    spec = _spec_for(tid, rev)
    del spec["task_id"]
    task_map = tmp_path / "task_map.json"
    task_map.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(task_map)
    client = FakeWrapper()
    report, store = _run(db, tmp_path, dispatch_policy, client)

    rec = next(r for r in report["results"] if r["task_id"] == tid)
    assert rec["action"] == "review", rec
    assert client.calls and client.calls[0]["task_id"] == tid
    # Agreed execution metadata shape rode the request.
    assert client.calls[0]["execution"]["route"] == "worker.code.standard"
    assert client.calls[0]["execution"]["base_revision"] == rev
    # Card is in review, NOT done.
    task = bridge.call("get_task", task_id=tid)["task"]
    assert task["status"] == "review"
    # Receipt is review-state with run ids.
    receipts = DispatchStore(store).for_task(tid)
    assert receipts[0].state == "review"
    assert receipts[0].run_id and receipts[0].attempt_id
    # Worktree was created deterministically.
    assert receipts[0].worktree and os.path.isdir(receipts[0].worktree)


def test_duplicate_poll_does_not_redispatch(board, tmp_path, dispatch_policy):
    db, bridge = board
    data, rev = dispatch_policy
    tid = bridge.call("create_task", title="c", assignee="jev-native",
                      body="x")["task_id"]
    spec = _spec_for(tid, rev)
    del spec["task_id"]
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(tm)
    client = FakeWrapper()
    _run(db, tmp_path, dispatch_policy, client)
    # Card left 'ready' scope (now review) — second tick can't see it anyway,
    # but even force-seeing it must not create a second reservation.
    report2, store = _run(db, tmp_path, dispatch_policy, FakeWrapper())
    assert all(r.get("action") != "review" for r in report2["results"])
    assert len(DispatchStore(store).for_task(tid)) == 1


def test_dependency_undone_skips(board, tmp_path, dispatch_policy):
    db, bridge = board
    data, rev = dispatch_policy
    parent = bridge.call("create_task", title="parent",
                         assignee="jev-native", body="p")["task_id"]
    child = bridge.call("create_task", title="child", assignee="jev-native",
                        body="c", parents=[parent])["task_id"]
    task = bridge.call("get_task", task_id=child)["task"]
    assert task["status"] == "todo"  # kernel demoted: parent not done
    # A todo card is out of the ready-only dispatch scope.
    report, _ = _run(db, tmp_path, dispatch_policy, FakeWrapper())
    assert not any(r["task_id"] == child and r.get("dispatched")
                   for r in report["results"])


def test_done_dependency_without_integrated_revision_blocks(
    board, tmp_path, dispatch_policy
):
    db, bridge = board
    data, rev = dispatch_policy
    parent = bridge.call("create_task", title="parent",
                         assignee="jev-native", body="p")["task_id"]
    # Complete the parent directly (no integrated_revision recorded).
    claim = bridge.call("claim", task_id=parent, claimer="jev:test")
    bridge.call("complete", task_id=parent, result="done",
                expected_run_id=claim["task"]["current_run_id"])
    child = bridge.call("create_task", title="child", assignee="jev-native",
                        body="c", parents=[parent])["task_id"]
    assert bridge.call("get_task", task_id=child)["task"]["status"] == "ready"
    spec = _spec_for(child, rev, dependency_ids=[parent])
    del spec["task_id"]
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps({child: spec}))
    data["task_map"] = str(tm)
    report, store = _run(db, tmp_path, dispatch_policy, FakeWrapper())
    rec = next(r for r in report["results"] if r["task_id"] == child)
    assert rec["action"] == "blocked"
    assert "integrated_revision" in rec["reason"]
    assert bridge.call("get_task", task_id=child)["task"]["status"] == "blocked"


def test_dependency_integrated_revision_ancestor_passes(
    board, tmp_path, dispatch_policy
):
    db, bridge = board
    data, rev = dispatch_policy
    parent = bridge.call("create_task", title="parent",
                         assignee="jev-native", body="p")["task_id"]
    claim = bridge.call("claim", task_id=parent, claimer="jev:test")
    bridge.call(
        "complete", task_id=parent, result="done",
        metadata={"integrated_revision": rev},  # the base commit itself
        expected_run_id=claim["task"]["current_run_id"],
    )
    child = bridge.call("create_task", title="child", assignee="jev-native",
                        body="c", parents=[parent])["task_id"]
    spec = _spec_for(child, rev, dependency_ids=[parent])
    del spec["task_id"]
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps({child: spec}))
    data["task_map"] = str(tm)
    report, _ = _run(db, tmp_path, dispatch_policy, FakeWrapper())
    rec = next(r for r in report["results"] if r["task_id"] == child)
    assert rec["action"] == "review", rec


def test_nonancestor_integrated_revision_blocks(
    board, tmp_path, dispatch_policy
):
    db, bridge = board
    data, rev = dispatch_policy
    # A commit that exists in git history but is NOT an ancestor of base:
    # a second root commit on an orphan branch of the same repo.
    import subprocess
    repo = data["workspaces"]["ws-main"]["repo"]
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "checkout", "-q", "--orphan", "foreign"],
                   cwd=repo, env=env, check=True, capture_output=True)
    subprocess.run(["git", "rm", "-rfq", "."], cwd=repo, env=env, check=True,
                   capture_output=True)
    Path(repo, "foreign.txt").write_text("foreign\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", "foreign"], cwd=repo, env=env,
                   check=True, capture_output=True)
    other_rev = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, env=env, check=True,
        capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "master"], cwd=repo, env=env,
                   check=True, capture_output=True)
    assert other_rev != rev

    parent = bridge.call("create_task", title="parent",
                         assignee="jev-native", body="p")["task_id"]
    claim = bridge.call("claim", task_id=parent, claimer="jev:test")
    bridge.call(
        "complete", task_id=parent, result="done",
        metadata={"integrated_revision": other_rev},
        expected_run_id=claim["task"]["current_run_id"],
    )
    child = bridge.call("create_task", title="child", assignee="jev-native",
                        body="c", parents=[parent])["task_id"]
    spec = _spec_for(child, rev, dependency_ids=[parent])
    del spec["task_id"]
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps({child: spec}))
    data["task_map"] = str(tm)
    report, _ = _run(db, tmp_path, dispatch_policy, FakeWrapper())
    rec = next(r for r in report["results"] if r["task_id"] == child)
    assert rec["action"] == "blocked"
    assert "ancestor" in rec["reason"]


def test_unsupported_effort_blocks_before_claim(board, tmp_path, dispatch_policy):
    db, bridge = board
    data, rev = dispatch_policy
    tid = bridge.call("create_task", title="c", assignee="jev-native",
                      body="x")["task_id"]
    spec = _spec_for(tid, rev, effort_hint="economy")  # devin has no map
    del spec["task_id"]
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(tm)
    client = FakeWrapper()
    report, _ = _run(db, tmp_path, dispatch_policy, client)
    rec = next(r for r in report["results"] if r["task_id"] == tid)
    assert rec["action"] == "blocked"
    assert "effort" in rec["reason"]
    assert client.calls == []  # never reached the wire
    # Card was blocked without ever being claimed by us.
    task = bridge.call("get_task", task_id=tid)["task"]
    assert task["status"] == "blocked"


def test_hard_tier_gates_approval(board, tmp_path, dispatch_policy):
    db, bridge = board
    data, rev = dispatch_policy
    data["routes"]["worker.code.hard"] = {"candidates": ["devin-swe-2-max"]}
    tid = bridge.call("create_task", title="c", assignee="jev-native",
                      body="x")["task_id"]
    spec = _spec_for(tid, rev, tier="hard")
    del spec["task_id"]
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(tm)
    client = FakeWrapper()
    report, store = _run(db, tmp_path, dispatch_policy, client)
    rec = next(r for r in report["results"] if r["task_id"] == tid)
    assert rec["action"] == "needs_approval"
    assert rec["approval_id"].startswith("a_")
    assert client.calls == []
    # Approval is durable.
    ap = DispatchStore(store).get_approval(rec["approval_id"])
    assert ap.state == "pending" and "op-test" in ap.allowed_actors


def test_verification_failure_blocks_quality_failed(
    board, tmp_path, dispatch_policy
):
    db, bridge = board
    data, rev = dispatch_policy
    tid = bridge.call("create_task", title="c", assignee="jev-native",
                      body="x")["task_id"]
    spec = _spec_for(tid, rev)
    spec["verification"] = {"argv": ["false"], "criteria": "exit 0"}
    del spec["task_id"]
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(tm)
    client = FakeWrapper()
    report, store = _run(db, tmp_path, dispatch_policy, client)
    rec = next(r for r in report["results"] if r["task_id"] == tid)
    assert rec["action"] == "blocked"
    assert "quality_failed" in rec["reason"]
    # No fallback attempt: exactly one submit happened and nothing else ran.
    assert len(client.calls) == 1
    assert bridge.call("get_task", task_id=tid)["task"]["status"] == "blocked"
    # The partial workspace is preserved for inspection, not cleaned up.
    receipt = DispatchStore(store).for_task(tid)[0]
    assert receipt.worktree and Path(receipt.worktree).is_dir()


def test_no_secrets_in_report_or_errors(board, tmp_path, dispatch_policy):
    """Credential material must never appear in reports, receipts, or card
    block reasons."""
    db, bridge = board
    data, rev = dispatch_policy
    data["execution"]["credential_file"] = str(tmp_path / "cred")
    (tmp_path / "cred").write_text("SECRET-TOKEN-xyz\n")
    tid = bridge.call("create_task", title="c", assignee="jev-native",
                      body="x")["task_id"]
    spec = _spec_for(tid, rev)
    del spec["task_id"]
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(tm)
    report, store = _run(db, tmp_path, dispatch_policy, FakeWrapper())
    blob = json.dumps(report)
    assert "SECRET-TOKEN" not in blob
    task = bridge.call("get_task", task_id=tid)["task"]
    assert "SECRET-TOKEN" not in json.dumps(task)


def test_missing_artifact_blocks(board, tmp_path, dispatch_policy):
    db, bridge = board
    data, rev = dispatch_policy
    tid = bridge.call("create_task", title="c", assignee="jev-native",
                      body="x")["task_id"]
    spec = _spec_for(tid, rev, artifacts=["nonexistent.out"])
    del spec["task_id"]
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(tm)
    report, _ = _run(db, tmp_path, dispatch_policy, FakeWrapper())
    rec = next(r for r in report["results"] if r["task_id"] == tid)
    assert rec["action"] == "blocked"
    assert "missing artifacts" in rec["reason"]


def test_failed_run_blocks_no_retry(board, tmp_path, dispatch_policy):
    db, bridge = board
    data, rev = dispatch_policy
    tid = bridge.call("create_task", title="c", assignee="jev-native",
                      body="x")["task_id"]
    spec = _spec_for(tid, rev)
    del spec["task_id"]
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(tm)
    report, _ = _run(db, tmp_path, dispatch_policy,
                     FakeWrapper(status="failed"))
    rec = next(r for r in report["results"] if r["task_id"] == tid)
    assert rec["action"] == "failed"
    task = bridge.call("get_task", task_id=tid)["task"]
    assert task["status"] == "blocked"


def test_restart_recovers_unknown(board, tmp_path, dispatch_policy):
    """A receipt left 'submitted' by a dead process becomes unknown + the card
    is blocked needs_input — never replayed."""
    db, bridge = board
    data, rev = dispatch_policy
    tid = bridge.call("create_task", title="c", assignee="jev-native",
                      body="x")["task_id"]
    store_path = tmp_path / "dispatch.db"
    store = DispatchStore(store_path)
    res = store.reserve(
        task_id=tid, task_revision="1", spec_hash="sha256:x",
        policy_fingerprint="sha256:p", workspace_id="ws-main",
        base_revision=rev, route="worker.code.standard",
    )
    store.transition(res.dispatch_id, "submitted", run_id="run_ghost")
    store.close()

    report, _ = _run(db, tmp_path, dispatch_policy, FakeWrapper())
    assert any(r["state"] == "unknown" for r in report["recovered"])
    store = DispatchStore(store_path)
    assert store.get(res.dispatch_id).state == "unknown"
    store.close()
    assert bridge.call("get_task", task_id=tid)["task"]["status"] == "blocked"
    # And the card is never re-dispatched.
    report2, _ = _run(db, tmp_path, dispatch_policy, FakeWrapper())
    assert not any(r["task_id"] == tid and r.get("dispatched")
                   for r in report2["results"])


def test_two_cards_same_workspace_serial(board, tmp_path, dispatch_policy):
    """Only one in-flight reservation per workspace: after the first card
    completes to review, the second can go."""
    db, bridge = board
    data, rev = dispatch_policy
    t1 = bridge.call("create_task", title="a", assignee="jev-native",
                     body="a")["task_id"]
    t2 = bridge.call("create_task", title="b", assignee="jev-native",
                     body="b")["task_id"]
    specs = {}
    for tid in (t1, t2):
        s = _spec_for(tid, rev)
        del s["task_id"]
        specs[tid] = s
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps(specs))
    data["task_map"] = str(tm)
    report, _ = _run(db, tmp_path, dispatch_policy, FakeWrapper())
    actions = {r["task_id"]: r["action"] for r in report["results"]}
    assert actions[t1] == "review" and actions[t2] == "review"


def test_max_retries_forced_to_one(board, tmp_path, dispatch_policy):
    db, bridge = board
    data, rev = dispatch_policy
    tid = bridge.call("create_task", title="c", assignee="jev-native",
                      body="x", max_retries=5)["task_id"]
    spec = _spec_for(tid, rev)
    del spec["task_id"]
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(tm)
    _run(db, tmp_path, dispatch_policy, FakeWrapper())
    task = bridge.call("get_task", task_id=tid)["task"]
    assert task["max_retries"] == 1
