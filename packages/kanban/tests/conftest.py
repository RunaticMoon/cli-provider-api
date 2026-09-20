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
    """A valid central routing policy (standard worker -> devin swe-2-max).

    Backend set mirrors the verified ops/ENVIRONMENT facts: devin swe-2-max
    (native, free), bai-flash on the official B.AI API (api transport,
    disabled pending canary, unknown cost), commandcode on its official API
    (api transport, declared but unrouted), and the disabled opus reviewer.
    """
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
            "cost_tiers": ["unknown"],
            "expiry_seconds": 3600,
        },
        "backends": [
            {
                "id": "devin-swe-2-max",
                "kind": "devin",
                "transport": "native",
                "model": "swe-2-max",
                "preset": "devin/swe-2-max",
                "driver": "devin",
                "enabled": True,
                "cost_tier": "free",
                "capabilities": {
                    "code": True, "review": True,
                    "research": False, "planning": False,
                },
            },
            {
                "id": "bai-flash",
                "kind": "bai",
                "transport": "api",
                "model": "deepseek-v4.1-flash",
                "preset": "hermes-api/bai-deepseek-v4.1-flash",
                "driver": "hermes-api",
                "enabled": False,
                "requires_canary": True,
                "cost_tier": "unknown",
                "disabled_reason": (
                    "B.AI official API authorized by correction 2444; kept "
                    "disabled until a real canary proves the wired path. "
                    "Verified model id deepseek-v4.1-flash at "
                    "https://api.b.ai/v1."
                ),
                "effort_map": {
                    "auto": "low", "economy": "low", "balanced": "high",
                    "thorough": "high", "maximum": "max",
                },
                "capabilities": {"code": True},
            },
            {
                "id": "commandcode-flash",
                "kind": "commandcode",
                "transport": "api",
                "model": "deepseek/deepseek-v4.1-flash",
                "preset": "hermes-api/cc-deepseek-v4.1-flash",
                "driver": "hermes-api",
                "enabled": False,
                "requires_canary": True,
                "cost_tier": "unknown",
                "disabled_reason": (
                    "CommandCode official provider API authorized by "
                    "correction 2444; disabled pending canary. Verified model "
                    "id deepseek/deepseek-v4.1-flash."
                ),
                "effort_map": {
                    "auto": "low", "economy": "low", "balanced": "high",
                    "thorough": "high", "maximum": "max",
                },
                "capabilities": {"code": True},
            },
            {
                "id": "devin-opus-review",
                "kind": "devin",
                "transport": "native",
                "model": "claude-opus-5-high",
                "preset": "devin/claude-opus-5-high",
                "driver": "devin",
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
            # Only routes actually used. easy orders BAI->Devin, standard
            # Devin->BAI; order is the compiler's input, never Jev's output.
            "worker.code.easy": {
                "candidates": ["bai-flash", "devin-swe-2-max"],
            },
            "worker.code.standard": {
                "candidates": ["devin-swe-2-max", "bai-flash"],
            },
            "reviewer.review.standard": {
                "candidates": ["devin-opus-review"],
            },
        },
        "workspaces": {
            "ws-main": {
                "repo": "/nonexistent-but-trusted",
                "worktree_root": "/nonexistent-but-trusted/.worktrees",
                "wrapper_workspace_id": "ws-alpha",
            },
        },
        "task_map": None,
        "control": {"operators": ["op-test"]},
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


# --- Dispatch-path fixtures ---------------------------------------------------


def make_git_repo(path: Path) -> tuple[Path, str]:
    """A real git repo with one commit; returns (repo_path, base_commit)."""
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
    )
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
    ):
        subprocess.run(argv, cwd=path, env=env, check=True, capture_output=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=path, env=env, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, env=env,
                   check=True, capture_output=True)
    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, env=env, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    return path, rev


class FakeWrapper:
    """RECORDING FAKE — mock-only stand-in for the wrapper client.

    Implements the WrapperClient call surface (submit_chat/get_run/
    cancel_run/get_artifact) with canned run views. Used to exercise the
    dispatcher's contract handling without any HTTP. Never asserted as proof
    of real wrapper behaviour.
    """

    def __init__(self, *, status: str = "completed", runs: dict | None = None):
        self.status = status
        self.calls: list[dict] = []
        self.cancelled: list[str] = []
        self._runs = runs or {}
        self._counter = 0

    def submit_chat(self, *, model, task_id, workspace_id, messages,
                    execution=None):
        from cli_provider_kanban.wrapper_client import SubmitOutcome

        self.calls.append({
            "model": model, "task_id": task_id, "workspace_id": workspace_id,
            "messages": messages, "execution": execution,
        })
        self._counter += 1
        run_id = f"run_{self._counter:04d}"
        run = {
            "run_id": run_id,
            "task_id": task_id,
            "attempt_id": f"att_{self._counter:04d}",
            "status": self.status,
            "workspace_id": workspace_id,
            "preset": model,
            "outcome": "completed" if self.status == "completed" else self.status,
            "summary": "fake completion" if self.status == "completed" else None,
            "artifacts": [],
            "detail": None,
            "cached": False,
        }
        self._runs[run_id] = run
        return SubmitOutcome(
            status=self.status, run=run, cached=False,
            content=run["summary"] or "",
        )

    def get_run(self, run_id):
        return self._runs.get(run_id)

    def cancel_run(self, run_id):
        self.cancelled.append(run_id)
        if run_id in self._runs:
            self._runs[run_id]["status"] = "cancelled"
        return {"run_id": run_id, "status": "cancelled",
                "requested": True, "confirmed": True, "detail": "fake"}

    def get_artifact(self, artifact_id):
        return b"fake-artifact"


@pytest.fixture
def git_repo(tmp_path):
    repo, rev = make_git_repo(tmp_path / "repo")
    return repo, rev


class StubWrapperServer:
    """A REAL loopback HTTP server (stdlib) standing in for the wrapper.

    Records every request and serves canned run views — the tests exercise
    the real ``WrapperClient`` over real HTTP; only the wrapper *process* is
    stubbed.
    """

    def __init__(self):
        import http.server
        import threading

        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _record(self, body=None):
                server.requests.append({
                    "method": self.command, "path": self.path,
                    "headers": dict(self.headers), "body": body,
                })

            def _send(self, code, obj, headers=None):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                self._record(body)
                if self.path == "/v1/chat/completions":
                    server.submit_count += 1
                    task_id = (body.get("metadata") or {}).get("task_id")
                    if server.reuse_run and task_id in server.by_task:
                        self._send(200, server.by_task[task_id][0],
                                   {"X-Run-Cached": "true"})
                        return
                    if server.submit_status_override is not None:
                        self._send(*server.submit_status_override)
                        return
                    run_id = f"run_{server.submit_count:04d}"
                    run = {
                        "run_id": run_id,
                        "task_id": task_id,
                        "attempt_id": f"att_{server.submit_count:04d}",
                        "status": server.run_status,
                        "workspace_id": (body.get("metadata") or {})
                        .get("workspace_id"),
                        "preset": body.get("model"),
                        "outcome": server.run_status,
                        "summary": "stub done",
                        "artifacts": [],
                        "detail": None,
                    }
                    resp = {
                        "id": "chatcmpl-stub",
                        "choices": [{"message": {"content": "stub done"}}],
                        "run": run,
                    }
                    server.runs[run_id] = run
                    server.by_task[task_id] = (resp, run_id)
                    self._send(200, resp)
                    return
                if self.path.endswith("/cancel"):
                    run_id = self.path.split("/")[-2]
                    run = server.runs.get(run_id)
                    if run is None:
                        self._send(404, {"error": {"message": "not found"}})
                        return
                    run["status"] = "cancelled"
                    self._send(200, {"run_id": run_id, "status": "cancelled",
                                     "requested": True, "confirmed": True,
                                     "detail": "stub"})
                    return
                self._send(404, {"error": {"message": "no stub route"}})

            def do_GET(self):
                self._record()
                if self.path.startswith("/api/v1/runs/"):
                    run_id = self.path.rsplit("/", 1)[-1]
                    run = server.runs.get(run_id)
                    if run is None:
                        self._send(404, {"error": {"message": "not found"}})
                    else:
                        self._send(200, run)
                    return
                if self.path.startswith("/api/v1/artifacts/"):
                    raw = b"artifact-bytes"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self._send(404, {"error": {"message": "no stub route"}})

        self.requests: list[dict] = []
        self.runs: dict = {}
        self.by_task: dict = {}
        self.submit_count = 0
        self.run_status = "completed"
        self.reuse_run = False
        self.submit_status_override = None
        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def stub_wrapper():
    server = StubWrapperServer()
    yield server
    server.close()


@pytest.fixture
def dispatch_policy(tmp_path, git_repo):
    """Policy wired at the real temp repo + a fake execution target."""
    repo, rev = git_repo
    data = policy_dict()
    data["workspaces"] = {
        "ws-main": {
            "repo": str(repo),
            "worktree_root": str(tmp_path / "worktrees"),
            "wrapper_workspace_id": "ws-alpha",
        },
    }
    data["execution"] = {
        "mode": "direct",
        "base_url": "http://127.0.0.1:9",  # never contacted (fake client)
        "model": "devin/swe-2-max",
    }
    data["verification"] = {
        "executables": {
            "true": "/usr/bin/true",
            "false": "/usr/bin/false",
            "touch": "/usr/bin/touch",
            "echo": "/usr/bin/echo",
        },
        "timeout_seconds": 30,
        "max_output_bytes": 8192,
        "max_diff_bytes": 65536,
    }
    data["control"] = {"operators": ["op-test"]}
    return data, rev
