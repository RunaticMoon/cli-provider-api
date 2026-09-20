"""Client for the Hermes kernel bridge.

Spawns ``hermes_bridge.py`` under the *installed Hermes interpreter* so every
board mutation goes through the real ``hermes_cli.kanban_db*`` kernel —
including its cross-process dispatch-tick lock — instead of this process
touching kanban.db directly. One bridge process per dispatch tick; the lock
is held for the tick's lifetime and released by process exit if we die.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

from .policy import HermesConfig

_BRIDGE = Path(__file__).with_name("hermes_bridge.py")


class KernelError(Exception):
    """Bridge failure: spawn error, protocol error, or kernel-side exception."""

    def __init__(self, message: str, *, error_type: str | None = None):
        super().__init__(message)
        self.error_type = error_type


def hermes_python(cfg: HermesConfig | None = None) -> str:
    """Resolve the installed Hermes interpreter."""
    if cfg is not None and cfg.python:
        return cfg.python
    env = os.environ.get("HERMES_PYTHON")
    if env:
        return env
    repo = hermes_repo(cfg)
    candidate = Path(repo) / "venv" / "bin" / "python"
    if candidate.is_file():
        return str(candidate)
    raise KernelError(
        f"installed Hermes interpreter not found at {candidate}; set "
        "policy hermes.python or HERMES_PYTHON"
    )


def hermes_repo(cfg: HermesConfig | None = None) -> str:
    """Resolve the installed Hermes source tree (for PYTHONPATH)."""
    return (
        (cfg.repo if cfg and cfg.repo else None)
        or os.environ.get("HERMES_AGENT_DIR")
        or str(Path.home() / ".hermes" / "hermes-agent")
    )


class KernelBridge:
    """One bridge process per dispatch tick. JSON-lines over pipes."""

    def __init__(
        self,
        board_db: str | Path,
        *,
        cfg: HermesConfig | None = None,
        env_extra: dict[str, str] | None = None,
    ):
        self.board_db = str(board_db)
        self._python = hermes_python(cfg)
        env = dict(os.environ)
        # The venv python needs the repo on sys.path to import hermes_cli
        # (same convention as the test fixtures).
        existing_pp = env.get("PYTHONPATH")
        repo = hermes_repo(cfg)
        env["PYTHONPATH"] = repo + (os.pathsep + existing_pp if existing_pp else "")
        if env_extra:
            env.update(env_extra)
        self._proc = subprocess.Popen(
            [self._python, str(_BRIDGE), self.board_db],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )
        self._lock = threading.Lock()
        self._closed = False
        ready = self._proc.stdout.readline()
        try:
            banner = json.loads(ready)
        except (json.JSONDecodeError, ValueError):
            self.close()
            raise KernelError(
                f"hermes bridge did not start cleanly for {self.board_db}"
            )
        if not banner.get("ok"):
            self.close()
            raise KernelError(
                f"hermes bridge init failed: {banner.get('error')}",
                error_type=banner.get("error_type"),
            )

    def call(self, op: str, **args) -> dict:
        """One op -> one result dict. Raises KernelError on kernel failure."""
        if self._closed or self._proc.poll() is not None:
            raise KernelError("hermes bridge process is not running")
        with self._lock:
            self._proc.stdin.write(json.dumps({"op": op, "args": args}) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
        if not line:
            raise KernelError(f"hermes bridge closed during op {op!r}")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise KernelError(
                f"kernel op {op} failed: {resp.get('error')}",
                error_type=resp.get("error_type"),
            )
        return resp["result"]

    def acquire_lock(self) -> bool:
        """Kernel-held dispatch singleton lock; False = another tick owns it."""
        return bool(self.call("acquire_lock")["held"])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc.poll() is None:
            try:
                proc.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        proc.stdin.close() if proc.stdin and not proc.stdin.closed else None
        proc.stdout.close() if proc.stdout and not proc.stdout.closed else None

    def __enter__(self) -> "KernelBridge":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["KernelBridge", "KernelError", "hermes_python"]
