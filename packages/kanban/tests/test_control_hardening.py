"""Control-plane hardening — OS-bound actors, disabled accept, safe resolve.

Parent-identified defects reproduced here:
- ``--actor`` was a bare allowlist string; it must bind to the current OS
  identity (``pwd.getpwuid(os.geteuid())``) AND the configured operator list.
- ``cmd_accept`` promoted review -> done on an arbitrary 40-hex claim; it is
  disabled in this slice (Lead verifies + completes via Hermes itself).
- ``cmd_resolve`` auto-aborted on a missing run — absence is NOT proof that
  nothing executed; unknown stays unknown pending external investigation.
- ``cmd_resolve`` on a completed run jumped straight to review, skipping the
  verification/artifact/cancellation checks the normal path applies.
- ``cmd_cancel`` must never claim confirmed cancellation when wrapper truth
  is unknown.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from cli_provider_kanban.control import (
    ControlError,
    cmd_accept,
    cmd_approve,
    cmd_cancel,
    cmd_resolve,
)
from cli_provider_kanban.dispatch import dispatch_once
from cli_provider_kanban.kernel import KernelBridge
from cli_provider_kanban.store import DispatchStore

from conftest import (  # noqa: F401
    CURRENT_OS_USER,
    dispatch_policy,
    requires_hermes,
    stub_wrapper,
    write_policy,
)
from test_dispatch import HERMES_ENV_KEYS, _spec_for  # noqa: F401

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


def _dispatch_card(env, tmp_path, *, spec_over=None):
    data, rev = env["policy_data"], env["rev"]
    tid = env["bridge"].call(
        "create_task", title="c", assignee="jev-native", body="x",
    )["task_id"]
    spec = _spec_for(tid, rev, **(spec_over or {}))
    del spec["task_id"]
    tm = tmp_path / f"tm_{tid}.json"
    tm.write_text(json.dumps({tid: spec}))
    env["policy_data"]["task_map"] = str(tm)
    env["policy_path"] = write_policy(tmp_path, env["policy_data"])
    report = dispatch_once(board_db=env["db"], policy_path=env["policy_path"],
                           store_path=env["store_path"])
    outcome = next(r for r in report["results"] if r["task_id"] == tid)
    receipts = DispatchStore(env["store_path"]).for_task(tid)
    return tid, outcome, (receipts[-1] if receipts else None)


# -- OS-bound operator identity ----------------------------------------------

def test_current_os_user_is_accepted(env, tmp_path):
    tid, _o, receipt = _dispatch_card(env, tmp_path)
    out = cmd_cancel(store_path=env["store_path"], board_db=env["db"],
                     policy_path=_policy_path(env, tmp_path),
                     dispatch_id=receipt.dispatch_id, actor=CURRENT_OS_USER)
    assert out["cancel_requested"] is True


def test_configured_but_foreign_actor_denied(env, tmp_path):
    """'op-test' is on the operator allowlist but is NOT this OS user — a
    bare --actor string must not authenticate."""
    tid, _o, receipt = _dispatch_card(env, tmp_path)
    with pytest.raises(ControlError, match="OS identity|not a configured"):
        cmd_cancel(store_path=env["store_path"], board_db=env["db"],
                   policy_path=_policy_path(env, tmp_path),
                   dispatch_id=receipt.dispatch_id, actor="op-test")
    assert not DispatchStore(env["store_path"]).get(
        receipt.dispatch_id).cancel_requested


def test_explicit_uid_map_default_deny(env, tmp_path):
    """When control.operator_uids is set it is authoritative: the actor must
    map to the current euid."""
    tid, _o, receipt = _dispatch_card(env, tmp_path)
    import os
    env["policy_data"]["control"]["operator_uids"] = {
        CURRENT_OS_USER: os.geteuid(),
        "op-test": os.geteuid() + 1,   # mapped to a DIFFERENT uid
    }
    env["policy_path"] = write_policy(tmp_path, env["policy_data"])
    # Correct mapping works.
    out = cmd_cancel(store_path=env["store_path"], board_db=env["db"],
                     policy_path=env["policy_path"],
                     dispatch_id=receipt.dispatch_id, actor=CURRENT_OS_USER)
    assert out["cancel_requested"] is True
    # A configured name mapped to another uid is denied.
    tid2, _o2, receipt2 = _dispatch_card(env, tmp_path)
    with pytest.raises(ControlError):
        cmd_cancel(store_path=env["store_path"], board_db=env["db"],
                   policy_path=env["policy_path"],
                   dispatch_id=receipt2.dispatch_id, actor="op-test")


# -- accept disabled ----------------------------------------------------------

def test_accept_is_disabled(env, tmp_path):
    tid, _o, receipt = _dispatch_card(env, tmp_path)
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        == "review"
    with pytest.raises(ControlError, match="disabled"):
        cmd_accept(store_path=env["store_path"], board_db=env["db"],
                   policy_path=_policy_path(env, tmp_path),
                   task_id=tid, actor=CURRENT_OS_USER,
                   integrated_revision=env["rev"])
    # Never done.
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        == "review"


def test_accept_disabled_even_without_receipt(env, tmp_path):
    with pytest.raises(ControlError, match="disabled"):
        cmd_accept(store_path=env["store_path"], board_db=env["db"],
                   policy_path=_policy_path(env, tmp_path),
                   task_id="t_nope", actor=CURRENT_OS_USER,
                   integrated_revision=env["rev"])


# -- resolve never auto-aborts -------------------------------------------------

def test_resolve_without_run_id_stays_unknown(env, tmp_path):
    """No run id on the receipt -> the execution may never have been
    submitted; absence of evidence is not proof of absence."""
    tid = env["bridge"].call(
        "create_task", title="c", assignee="jev-native", body="x",
    )["task_id"]
    store = DispatchStore(env["store_path"])
    res = store.reserve(task_id=tid, task_revision="1",
                        spec_hash="sha256:x", policy_fingerprint="sha256:p",
                        workspace_id="ws-main", base_revision=env["rev"],
                        route="worker.code.standard")
    store.transition(res.dispatch_id, "claimed")
    store.transition(res.dispatch_id, "unknown", detail="lost")
    store.close()
    out = cmd_resolve(store_path=env["store_path"], board_db=env["db"],
                      policy_path=_policy_path(env, tmp_path),
                      dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    assert out["resolved"] == "none"
    store = DispatchStore(env["store_path"])
    assert store.get(res.dispatch_id).state == "unknown"
    store.close()


def test_resolve_wrapper_404_stays_unknown(env, tmp_path):
    """A clean 404 is not proof nothing executed — never auto-abort."""
    tid = env["bridge"].call(
        "create_task", title="c", assignee="jev-native", body="x",
    )["task_id"]
    store = DispatchStore(env["store_path"])
    res = store.reserve(task_id=tid, task_revision="1",
                        spec_hash="sha256:x", policy_fingerprint="sha256:p",
                        workspace_id="ws-main", base_revision=env["rev"],
                        route="worker.code.standard")
    store.transition(res.dispatch_id, "submitted", run_id="run_ghost")
    store.transition(res.dispatch_id, "unknown", detail="crash")
    store.close()
    out = cmd_resolve(store_path=env["store_path"], board_db=env["db"],
                      policy_path=_policy_path(env, tmp_path),
                      dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    assert out["resolved"] == "none"
    store = DispatchStore(env["store_path"])
    assert store.get(res.dispatch_id).state == "unknown"
    store.close()


def _crashed_receipt(env, tmp_path, *, tid, run_id, worktree):
    """Simulate a dispatch that crashed after submit: a real claimed card on
    the board plus an ``unknown`` receipt pointing at ``run_id``."""
    bridge = env["bridge"]
    bridge.call("set_max_retries", task_id=tid, value=1)
    claim = bridge.call("claim", task_id=tid, claimer="jev:test",
                        ttl_seconds=600)
    krun = claim["task"]["current_run_id"]
    store = DispatchStore(env["store_path"])
    res = store.reserve(task_id=tid, task_revision="1",
                        spec_hash="sha256:x", policy_fingerprint="sha256:p",
                        workspace_id="ws-main", base_revision=env["rev"],
                        route="worker.code.standard")
    store.transition(res.dispatch_id, "claimed", kernel_run_id=krun,
                     worktree=worktree)
    store.transition(res.dispatch_id, "submitted", run_id=run_id)
    store.transition(res.dispatch_id, "unknown", detail="crash after submit")
    store.close()
    return res


def _stub_run_for(env, tid, rev, policy_version):
    """Create a real completed run on the stub for ``tid``."""
    from cli_provider_kanban.wrapper_client import WrapperClient
    client = WrapperClient(env["stub"].base_url)
    return client.submit_chat(
        model="devin/swe-2-max", task_id=tid, workspace_id="ws-alpha",
        messages=[],
        execution={
            "task_revision": "1",
            "base_revision": rev,
            "route": "worker.code.standard",
            "policy_version": policy_version,
        },
    )


def _card_with_spec(env, tmp_path):
    data, rev = env["policy_data"], env["rev"]
    tid = env["bridge"].call(
        "create_task", title="c", assignee="jev-native", body="x",
    )["task_id"]
    spec = _spec_for(tid, rev)
    del spec["task_id"]
    tm = tmp_path / f"tm_{tid}.json"
    tm.write_text(json.dumps({tid: spec}))
    env["policy_data"]["task_map"] = str(tm)
    env["policy_path"] = write_policy(tmp_path, env["policy_data"])
    return tid


def test_resolve_completed_requires_verification(env, tmp_path):
    """A completed run whose worktree is gone cannot jump to review —
    resolve applies the SAME checks as the normal dispatch path."""
    data, rev = env["policy_data"], env["rev"]
    tid = _card_with_spec(env, tmp_path)
    run = _stub_run_for(env, tid, rev, data["policy_version"])
    res = _crashed_receipt(env, tmp_path, tid=tid, run_id=run.run_id,
                           worktree=str(tmp_path / "gone"))
    with pytest.raises(ControlError):
        cmd_resolve(store_path=env["store_path"], board_db=env["db"],
                    policy_path=env["policy_path"],
                    dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    store = DispatchStore(env["store_path"])
    receipt = store.get(res.dispatch_id)
    store.close()
    assert receipt.state == "unknown"
    # The card was blocked for a human — never promoted to review.
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        == "blocked"


def test_resolve_completed_happy_path_reviews(env, tmp_path):
    """Unknown receipt + a real completed run + intact prepared worktree ->
    verification reruns and the card reaches review."""
    data, rev = env["policy_data"], env["rev"]
    tid = _card_with_spec(env, tmp_path)
    run = _stub_run_for(env, tid, rev, data["policy_version"])
    prepared = data["workspaces"]["ws-main"]["prepared_worktree"]
    res = _crashed_receipt(env, tmp_path, tid=tid, run_id=run.run_id,
                           worktree=prepared)
    out = cmd_resolve(store_path=env["store_path"], board_db=env["db"],
                      policy_path=env["policy_path"],
                      dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    assert out["resolved"] == "review"
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        == "review"


def test_resolve_completed_outcome_partial_does_not_review(env, tmp_path):
    """completed but outcome!=succeeded must not promote even in resolve."""
    data, rev = env["policy_data"], env["rev"]
    tid = _card_with_spec(env, tmp_path)
    run = _stub_run_for(env, tid, rev, data["policy_version"])
    env["stub"].runs[run.run_id]["outcome"] = "partial"
    prepared = data["workspaces"]["ws-main"]["prepared_worktree"]
    res = _crashed_receipt(env, tmp_path, tid=tid, run_id=run.run_id,
                           worktree=prepared)
    out = cmd_resolve(store_path=env["store_path"], board_db=env["db"],
                      policy_path=env["policy_path"],
                      dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    assert out["resolved"] in ("failed", "blocked", "none")
    assert env["bridge"].call("get_task", task_id=tid)["task"]["status"] \
        != "review"


# -- cancel honesty ------------------------------------------------------------

def test_cancel_unknown_run_reports_unconfirmed(env, tmp_path):
    """A 404 from the wrapper is 'not found', NOT a confirmed cancel."""
    tid = env["bridge"].call(
        "create_task", title="c", assignee="jev-native", body="x",
    )["task_id"]
    store = DispatchStore(env["store_path"])
    res = store.reserve(task_id=tid, task_revision="1",
                        spec_hash="sha256:x", policy_fingerprint="sha256:p",
                        workspace_id="ws-main", base_revision=env["rev"],
                        route="worker.code.standard")
    store.transition(res.dispatch_id, "submitted", run_id="run_ghost")
    store.close()
    out = cmd_cancel(store_path=env["store_path"], board_db=env["db"],
                     policy_path=_policy_path(env, tmp_path),
                     dispatch_id=res.dispatch_id, actor=CURRENT_OS_USER)
    assert out["wrapper"]["status"] == "not_found"
    assert out["wrapper"]["confirmed"] is not True
    store = DispatchStore(env["store_path"])
    assert store.get(res.dispatch_id).state == "cancelled"
    store.close()


def test_cancel_during_verification_never_reaches_review(
    env, tmp_path
):
    """Concurrency: cancel while verification is running must stop the
    review handoff via the atomic receipt guard."""
    tid, _o, receipt = _dispatch_card_slow(env, tmp_path)
    # _dispatch_card_slow runs dispatch in a thread while the card's
    # verification sleeps; cancel lands mid-verify.
    receipt = DispatchStore(env["store_path"]).for_task(tid)[0]
    assert receipt.state == "cancelled"
    task = env["bridge"].call("get_task", task_id=tid)["task"]
    assert task["status"] != "review" and task["status"] != "done"


def _dispatch_card_slow(env, tmp_path):
    """Dispatch in a thread with a slow verification; cancel mid-verify."""
    data, rev = env["policy_data"], env["rev"]
    tid = env["bridge"].call(
        "create_task", title="slow", assignee="jev-native", body="x",
    )["task_id"]
    spec = _spec_for(tid, rev,
                     verification={"argv": ["sleep", "5"],
                                   "criteria": "exit 0"})
    del spec["task_id"]
    tm = tmp_path / f"tm_{tid}.json"
    tm.write_text(json.dumps({tid: spec}))
    env["policy_data"]["task_map"] = str(tm)
    env["policy_path"] = write_policy(tmp_path, env["policy_data"])

    outcome_box: list[dict] = []

    def _tick():
        report = dispatch_once(
            board_db=env["db"], policy_path=env["policy_path"],
            store_path=env["store_path"])
        outcome_box.append(
            next(r for r in report["results"] if r["task_id"] == tid))

    thread = threading.Thread(target=_tick, daemon=True)
    thread.start()
    # Wait until the receipt is submitted/completing (verification in flight
    # runs under the 'completing' state).
    deadline = time.time() + 15
    dispatch_id = None
    while time.time() < deadline:
        store = DispatchStore(env["store_path"])
        receipts = store.for_task(tid)
        store.close()
        if receipts and receipts[-1].state in ("submitted", "completing"):
            dispatch_id = receipts[-1].dispatch_id
            break
        time.sleep(0.05)
    assert dispatch_id is not None, "dispatch never reached submitted"
    out = cmd_cancel(store_path=env["store_path"], board_db=env["db"],
                     policy_path=env["policy_path"],
                     dispatch_id=dispatch_id, actor=CURRENT_OS_USER)
    assert out["cancel_requested"] is True
    thread.join(timeout=30)
    assert outcome_box, "dispatch thread never finished"
    assert outcome_box[0]["action"] == "cancelled"
    return tid, outcome_box[0], None
