#!/usr/bin/env python3
"""Synthetic ``hermes`` stand-in for driver unit tests.

Speaks the verified ``hermes chat --format stream-json`` protocol:
``system/init`` → ``text`` / ``tool_use`` / ``tool_result`` → terminal
``result``. Behaviour is selected by ``FAKE_HERMES_MODE`` (operator env on the
test executor), never by the run request. No real CLI, account or network.

The fixture also records what the driver handed it — argv, cwd, stdin file,
HERMES_HOME and the generated config.yaml — into ``FAKE_HERMES_CAPTURE`` so
tests can assert on argv/env/config without ever seeing a real secret value.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

MODE = os.environ.get("FAKE_HERMES_MODE", "ok")
CAPTURE = os.environ.get("FAKE_HERMES_CAPTURE")
PIDFILE = os.environ.get("FAKE_HERMES_PIDFILE")
READY = os.environ.get("FAKE_HERMES_READYFILE")

ANSWER = "fixture-answer-text"
PLANNING_SECRET = "SECRET-PLANNING-TEXT"
STDERR_TOKEN = "SECRET-STDERR-TOKEN"
ERR_TOKEN = "sk-fixturesecret-deadbeef"


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def init_frame(model: str | None = None) -> None:
    if model is None:
        argv = sys.argv[1:]
        model = argv[argv.index("--model") + 1] if "--model" in argv else "?"
    emit({"type": "system", "subtype": "init", "model": model,
          "session_id": "fake-session", "timestamp": 1})


def result_frame(exit_code: int = 0, text: str = ANSWER, error: str | None = None,
                 tokens: dict | None = None) -> None:
    payload = {"type": "result", "session_id": "fake-session",
               "exit_code": exit_code, "text": text,
               "tokens": tokens or {"input": 3, "output": 2, "total": 5,
                                    "cache_read": 0, "cache_write": 0},
               "duration_ms": 1, "timestamp": 2}
    if error is not None:
        payload["error"] = error
    emit(payload)


def capture(argv: list[str]) -> None:
    """Record argv/env/config for assertions; never includes secret values."""
    if not CAPTURE:
        return
    home = os.environ.get("HERMES_HOME", "")
    config_text = ""
    if home:
        try:
            with open(os.path.join(home, "config.yaml"), encoding="utf-8") as fh:
                config_text = fh.read()
        except OSError:
            config_text = "<unreadable>"
    query_text = ""
    if "--query-file" in argv:
        try:
            with open(argv[argv.index("--query-file") + 1], encoding="utf-8") as fh:
                query_text = fh.read()
        except OSError:
            query_text = "<unreadable>"
    # Env allowlist only: dumping os.environ wholesale could leak test secrets.
    env_seen = {k: bool(os.environ.get(k)) for k in
                ("HERMES_HOME", "BAI_API_KEY", "COMMANDCODE_API_KEY",
                 "LEAD_SESSION_TOKEN", "DEVIN_CLI", "OPENAI_API_KEY")}
    with open(CAPTURE, "w", encoding="utf-8") as fh:
        json.dump({"argv": argv, "cwd": os.getcwd(), "hermes_home": home,
                   "config": config_text, "query": query_text,
                   "env_seen": env_seen}, fh)


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        if MODE == "bad_version":
            print("unparseable")
            return 0
        print("Hermes Agent v0.21.3 (2026.9.14) · upstream d86a1687")
        return 0

    capture(argv)

    if MODE == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if READY:
            open(READY, "w").write("ready")
        if PIDFILE:
            child = subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(120)"])
            with open(PIDFILE, "w") as fh:
                fh.write(str(child.pid))
        init_frame()
        time.sleep(120)
        return 0

    if MODE == "hang":
        init_frame()
        time.sleep(120)
        return 0

    if MODE == "no_init":
        result_frame()
        return 0

    if MODE == "malformed":
        init_frame()
        sys.stdout.write("{not valid json\n")
        sys.stdout.flush()
        return 0

    if MODE == "oversize":
        init_frame()
        sys.stdout.write('{"type":"text","text":"' + "x" * 200_000 + '"}\n')
        sys.stdout.flush()
        return 0

    if MODE == "unknown_event":
        init_frame()
        emit({"type": "progress", "note": "unknown"})
        result_frame()
        return 0

    if MODE == "no_result":
        init_frame()
        emit({"type": "text", "text": "partial"})
        return 0

    if MODE == "model_mismatch":
        init_frame(model="other-model-9")
        result_frame()
        return 0

    if MODE == "error_result":
        init_frame()
        result_frame(exit_code=1, text="", error="provider exploded " + ERR_TOKEN)
        return 1

    if MODE == "nonzero_exit":
        init_frame()
        emit({"type": "text", "text": "worked"})
        result_frame(exit_code=1, text="worked")
        return 1

    if MODE == "tool_error":
        init_frame()
        emit({"type": "tool_use", "name": "terminal",
              "tool_call_id": "t1", "input": {"cmd": "false"}})
        emit({"type": "tool_result", "name": "terminal", "tool_call_id": "t1",
              "output": "exit 1", "is_error": True})
        emit({"type": "text", "text": ANSWER})
        result_frame()
        return 0

    if MODE == "stderr_noise":
        sys.stderr.write(STDERR_TOKEN + "\n")
        sys.stderr.flush()
        init_frame()
        emit({"type": "text", "text": ANSWER})
        result_frame()
        return 0

    if MODE == "zero_usage":
        init_frame()
        emit({"type": "text", "text": ANSWER})
        result_frame(tokens={"input": 0, "output": 0, "total": 0,
                             "cache_read": 0, "cache_write": 0})
        return 0

    # default: a normal run with one tool call and streamed answer text
    init_frame()
    emit({"type": "tool_use", "name": "read_file",
          "tool_call_id": "t1", "input": {"path": "input.txt"}})
    emit({"type": "tool_result", "name": "read_file", "tool_call_id": "t1",
          "output": "file-bytes", "is_error": False})
    emit({"type": "text", "text": ANSWER[:10]})
    emit({"type": "text", "text": ANSWER[10:]})
    result_frame()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
