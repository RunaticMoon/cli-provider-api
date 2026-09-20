"""Standalone CLI: --help, schema, shadow commands actually run."""

from __future__ import annotations

import json

import pytest

from cli_provider_kanban.__main__ import main

from conftest import insert_task, policy_dict, spec_body, write_policy


def test_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "shadow" in out and "schema" in out


def test_schema_command_prints_json_schemas(capsys):
    assert main(["schema"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert "task_spec" in doc
    assert "jev_decision" in doc
    assert "policy" in doc
    assert doc["task_spec"]["type"] == "object"


def test_schema_command_out_file(tmp_path):
    out = tmp_path / "schemas.json"
    assert main(["schema", "--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert "jev_decision" in doc


def test_shadow_command_end_to_end(board_db, tmp_path, capsys):
    insert_task(board_db, task_id="t_abc12345", body=spec_body())
    out = tmp_path / "report.json"
    cache = tmp_path / "cache.json"
    rc = main([
        "shadow",
        "--board-db", str(board_db),
        "--policy", str(write_policy(tmp_path)),
        "--out", str(out),
        "--cache", str(cache),
    ])
    assert rc == 0
    report = json.loads(out.read_text())
    assert report["records"][0]["decision"]["recommended_action"] == "execute"
    assert cache.is_file()


def test_shadow_missing_board_db_fails(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main([
            "shadow",
            "--board-db", str(tmp_path / "nope.db"),
            "--policy", str(write_policy(tmp_path)),
            "--out", str(tmp_path / "out.json"),
        ])
    assert exc.value.code != 0


def test_shadow_invalid_policy_fails(tmp_path, board_db):
    bad = policy_dict()
    bad["backends"] = [{"id": "codex", "kind": "codex"}]
    with pytest.raises(SystemExit) as exc:
        main([
            "shadow",
            "--board-db", str(board_db),
            "--policy", str(write_policy(tmp_path, bad)),
            "--out", str(tmp_path / "out.json"),
        ])
    assert exc.value.code != 0


def test_shadow_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["shadow", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--board-db" in out and "--policy" in out


# --- Slice 5: dispatch / status / control / compile contracts -----------------

from conftest import (  # noqa: E402
    dispatch_policy,  # noqa: F401  (fixture)
    requires_hermes,
    stub_wrapper,  # noqa: F401  (fixture)
)
from test_dispatch import HERMES_ENV_KEYS, _spec_for  # noqa: E402


def test_help_lists_all_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    out = capsys.readouterr().out
    for cmd in ("schema", "shadow", "dispatch", "status", "control",
                "compile"):
        assert cmd in out


def test_schema_decision_is_route_only(capsys):
    assert main(["schema"]) == 0
    doc = json.loads(capsys.readouterr().out)
    props = doc["jev_decision"]["properties"]
    assert "candidates" not in props  # Jev never selects backends
    assert "route" in props


def test_dispatch_requires_once(tmp_path, capsys):
    policy = write_policy(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["dispatch", "--board-db", str(tmp_path / "k.db"),
              "--policy", str(policy), "--store", str(tmp_path / "d.db")])
    assert exc.value.code == 2
    assert "--once" in capsys.readouterr().err


def test_compile_dry_run(tmp_path, capsys):
    data = policy_dict()
    data["execution"] = {"mode": "direct", "base_url": "http://127.0.0.1:9",
                         "model": "devin/swe-2-max"}
    policy = write_policy(tmp_path, data)
    assert main(["compile", "--policy", str(policy)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["combos"]
    assert "SECRET" not in json.dumps(plan)


def test_compile_apply_requires_credential_file(tmp_path, capsys):
    policy = write_policy(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["compile", "--policy", str(policy), "--apply", "local"])
    assert exc.value.code == 2
    assert "credential" in capsys.readouterr().err


@requires_hermes
def test_cli_dispatch_status_control_roundtrip(
    tmp_path, monkeypatch, capsys, dispatch_policy, stub_wrapper
):
    """Full CLI vertical: dispatch -> status -> control cancel through
    main() argv — real kernel board + real loopback stub wrapper."""
    for key in HERMES_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    data, rev = dispatch_policy
    data["execution"]["base_url"] = stub_wrapper.base_url
    board = tmp_path / "kanban.db"
    store = tmp_path / "dispatch.db"

    from cli_provider_kanban.kernel import KernelBridge
    bridge = KernelBridge(board, env_extra={"HERMES_HOME": str(tmp_path / "hh")})
    tid = bridge.call("create_task", title="cli card",
                      assignee="jev-native", body="x")["task_id"]
    spec = _spec_for(tid, rev)
    del spec["task_id"]
    tm = tmp_path / "tm.json"
    tm.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(tm)
    policy = write_policy(tmp_path, data, "policy2.yaml")
    bridge.close()

    rc = main(["dispatch", "--once", "--board-db", str(board),
               "--policy", str(policy), "--store", str(store)])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    rec = next(r for r in report["results"] if r["task_id"] == tid)
    assert rec["action"] == "review"

    rc = main(["status", "--board-db", str(board), "--policy", str(policy),
               "--store", str(store), "--task-id", tid])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["receipt"]["state"] == "review"
    did = out["receipt"]["dispatch_id"]

    rc = main(["control", "--board-db", str(board), "--policy", str(policy),
               "--store", str(store), "--actor", "op-test",
               "cancel", "--dispatch-id", did])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cancel_requested"] is True


@requires_hermes
def test_cli_control_unauthorized_exits_2(
    tmp_path, monkeypatch, capsys, dispatch_policy, stub_wrapper
):
    for key in HERMES_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    data, rev = dispatch_policy
    data["execution"]["base_url"] = stub_wrapper.base_url
    policy = write_policy(tmp_path, data)
    board = tmp_path / "kanban.db"
    store = tmp_path / "dispatch.db"
    with pytest.raises(SystemExit) as exc:
        main(["control", "--board-db", str(board), "--policy", str(policy),
              "--store", str(store), "--actor", "mallory",
              "cancel", "--dispatch-id", "d_nope"])
    assert exc.value.code == 2
    assert "not a configured operator" in capsys.readouterr().err
