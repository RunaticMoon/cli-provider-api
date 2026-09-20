"""Shared fixtures for the kanban/Jev slice.

Board fixtures come in two flavours:

* ``make_board_db`` — a throwaway SQLite file carrying exactly the columns the
  read-only shadow reader selects (a subset of the real Hermes schema). Used by
  the fast unit tests so they never touch a live Hermes tree.
* ``run_hermes`` — drives the *actually installed* Hermes venv interpreter
  against a scratch ``HERMES_HOME``/``HERMES_KANBAN_DB`` to build boards with
  the real ``hermes_cli.kanban_db`` API and to run the stock ``dispatch_once``.
  Skipped when the venv is absent; never touches the real ``~/.hermes``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

# Columns mirroring the real ``tasks`` schema that the shadow reader selects.
_TASKS_DDL = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT,
    assignee TEXT,
    status TEXT NOT NULL,
    priority INTEGER DEFAULT 0,
    created_by TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    workspace_kind TEXT NOT NULL DEFAULT 'scratch',
    workspace_path TEXT,
    branch_name TEXT,
    project_id TEXT,
    claim_lock TEXT,
    claim_expires INTEGER,
    tenant TEXT,
    result TEXT,
    idempotency_key TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid INTEGER,
    worker_started_at INTEGER,
    last_failure_error TEXT,
    max_runtime_seconds INTEGER,
    last_heartbeat_at INTEGER,
    current_run_id INTEGER,
    workflow_template_id TEXT,
    current_step_key TEXT,
    skills TEXT,
    model_override TEXT,
    provider_override TEXT,
    reasoning_effort TEXT,
    max_retries INTEGER,
    goal_mode INTEGER NOT NULL DEFAULT 0,
    goal_max_turns INTEGER,
    session_id TEXT,
    block_kind TEXT,
    block_recurrences INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id));
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_id INTEGER,
    kind TEXT NOT NULL,
    payload TEXT,
    created_at INTEGER NOT NULL
);
"""


def make_board_db(path: Path) -> Path:
    """Create a disposable board file with the columns the reader needs."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_TASKS_DDL)
        conn.commit()
    finally:
        conn.close()
    return path


def insert_task(
    db_path: Path,
    *,
    task_id: str,
    title: str = "card",
    body: str | None = None,
    assignee: str | None = "jev-native",
    status: str = "ready",
    priority: int = 0,
    workspace_kind: str = "scratch",
    workspace_path: str | None = None,
    model_override: str | None = None,
    provider_override: str | None = None,
    reasoning_effort: str | None = None,
    skills: str | None = None,
    max_retries: int | None = None,
    max_runtime_seconds: int | None = None,
    parents: tuple[str, ...] = (),
    created_at: int = 1_700_000_000,
) -> str:
    """Direct-INSERT a card row into a fixture board (no Hermes import)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, priority, "
            "created_by, created_at, workspace_kind, workspace_path, "
            "model_override, provider_override, reasoning_effort, skills, "
            "max_retries, max_runtime_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, 'test', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, title, body, assignee, status, priority, created_at,
                workspace_kind, workspace_path, model_override,
                provider_override, reasoning_effort, skills, max_retries,
                max_runtime_seconds,
            ),
        )
        for parent in parents:
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (parent, task_id),
            )
        conn.commit()
    finally:
        conn.close()
    return task_id


def policy_dict(**overrides) -> dict:
    """A valid central routing policy (standard worker -> devin swe-2-max)."""
    data: dict = {
        "schema_version": 1,
        "policy_version": "2026-09-20.1",
        "scope": {"assignee": "jev-native", "statuses": ["ready", "todo"]},
        "classifier": {"llm": "disabled"},
        "capabilities": ["code", "review", "research", "planning"],
        "decomposition": {"max_depth": 3, "max_children": 8, "replan_cap": 2},
        "limits": {
            "max_cards": 16,
            "max_body_bytes": 65536,
            "max_spec_bytes": 32768,
        },
        "approval": {
            "tiers": ["hard", "max"],
            "risk_flags": [
                "authn", "authz", "security", "billing", "destruction",
                "migration", "production", "external_effects",
            ],
        },
        "backends": [
            {
                "id": "devin-swe-2-max",
                "kind": "devin",
                "model": "swe-2-max",
                "enabled": True,
                "cost_tier": "free",
                "capabilities": {
                    "code": True, "review": True,
                    "research": False, "planning": False,
                },
            },
            {
                "id": "bai-code",
                "kind": "bai_code",
                "model": None,
                "enabled": False,
                "disabled_reason": (
                    "BAI direct-API historical policy is not permitted; "
                    "server binary absent on this host (ARM64 unsupported)"
                ),
                "capabilities": {"code": False},
            },
            {
                "id": "devin-opus-review",
                "kind": "devin",
                "model": "claude-opus-5-high",
                "enabled": False,
                "cost_tier": "high",
                "disabled_reason": (
                    "reviewer fallback inactive: persisted policy conflicts "
                    "with the active conversation policy pending resolution"
                ),
                "capabilities": {"review": True},
            },
        ],
        "routes": {
            "worker.code.free": {"candidates": ["devin-swe-2-max"]},
            "worker.code.easy": {"candidates": ["devin-swe-2-max"]},
            "worker.code.standard": {
                "candidates": ["devin-swe-2-max", "bai-code"],
            },
            "worker.code.hard": {"candidates": ["devin-swe-2-max"]},
            "worker.code.max": {"candidates": ["devin-swe-2-max"]},
            "reviewer.review.standard": {
                "candidates": ["devin-opus-review"],
            },
        },
        "workspaces": {"ws-main": {"path": "/nonexistent-but-trusted"}},
        "task_map": None,
    }
    data.update(overrides)
    return data


def write_policy(tmp_path: Path, data: dict | None = None, name: str = "policy.yaml") -> Path:
    path = tmp_path / name
    path.write_text(
        yaml.safe_dump(data if data is not None else policy_dict()),
        encoding="utf-8",
    )
    return path


def spec_dict(task_id: str = "t_abc12345", **overrides) -> dict:
    """A complete, valid TaskSpec for ``task_id`` (worker/code/standard)."""
    data: dict = {
        "task_id": task_id,
        "task_revision": "1",
        "role": "worker",
        "capability": "code",
        "tier": "standard",
        "effort_hint": "balanced",
        "objective": "Implement the bounded change described by the card",
        "inputs": ["task-shadow.md slice description"],
        "dependency_ids": [],
        "relevant_files": ["packages/kanban/src/cli_provider_kanban/models.py"],
        "allowed_scope": ["packages/kanban/"],
        "artifacts": ["patch", "pytest output"],
        "verification": {
            "argv": ["uv", "run", "pytest", "packages/kanban"],
            "criteria": "exit code 0",
        },
        "acceptance_criteria": ["new suite passes"],
        "prohibited": ["no changes outside packages/kanban"],
        "base_revision": "832c1dd7eeac965be0481119be86e57b1d532019",
        "workspace_id": "ws-main",
        "risk_flags": [],
        "replan_count": 0,
    }
    data.update(overrides)
    return data


def spec_body(spec: dict | None = None) -> str:
    """A card body carrying one fenced ``jev-task-spec`` JSON block."""
    return (
        "Human-readable card text.\n\n"
        "```jev-task-spec\n"
        + json.dumps(spec if spec is not None else spec_dict(), indent=2)
        + "\n```\n"
    )


# --- Real Hermes integration -------------------------------------------------

HERMES_REPO = Path(os.environ.get("HERMES_AGENT_DIR", "/home/ubuntu/.hermes/hermes-agent"))
HERMES_PYTHON = Path(
    os.environ.get("HERMES_PYTHON", str(HERMES_REPO / "venv" / "bin" / "python"))
)


def hermes_available() -> bool:
    return HERMES_PYTHON.is_file() and (HERMES_REPO / "hermes_cli").is_dir()


requires_hermes = pytest.mark.skipif(
    not hermes_available(), reason="installed Hermes venv not found"
)


def run_hermes(tmp_path: Path, script: str, *, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run ``script`` under the installed Hermes venv with scratch-only env.

    ``HERMES_HOME``, ``HERMES_KANBAN_HOME`` and ``HERMES_KANBAN_DB`` are all
    redirected into ``tmp_path``; the real profile tree is never touched.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(HERMES_REPO)
    env["HERMES_HOME"] = str(tmp_path / "hermes_home")
    env["HERMES_KANBAN_HOME"] = str(tmp_path / "kanban_home")
    env["HERMES_KANBAN_DB"] = str(tmp_path / "kanban.db")
    env.pop("HERMES_KANBAN_BOARD", None)
    return subprocess.run(
        [str(HERMES_PYTHON), "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture
def board_db(tmp_path: Path) -> Path:
    return make_board_db(tmp_path / "kanban.db")
