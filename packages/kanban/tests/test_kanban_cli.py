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
