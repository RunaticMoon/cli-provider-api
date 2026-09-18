#!/usr/bin/env python3
"""Synthetic ``agy`` stand-in for driver tests.

Operator-selected behaviour via ``FAKE_AGY_MODE`` (never by a run request). No
real CLI, account or network call happens here; every frame is a fixture.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

MODE = os.environ.get("FAKE_AGY_MODE", "ok")
PIDFILE = os.environ.get("FAKE_AGY_PIDFILE")

PLANNING_TEXT = "SECRET-PLANNING-TEXT"
CHECKPOINT_TEXT = "SECRET-CHECKPOINT-TEXT"
STDERR_TOKEN = "SECRET-STDERR-TOKEN"


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def init_frame() -> None:
    emit({"event": "init", "conversation_id": "fake-conversation", "init": {}})


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        if MODE == "bad_version":
            print("command-not-found-ish")
            return 0
        print("1.2.5")
        return 0

    if "--input-format" not in argv or "--output-format" not in argv:
        sys.stderr.write("fixture expects stream-json flags\n")
        return 2

    line = sys.stdin.readline()
    if not line:
        return 2

    if MODE == "ignore_term":
        # Install SIG_IGN *before* publishing readiness: a test that signals us
        # must never race the handler installation.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        ready = os.environ.get("FAKE_AGY_READYFILE")
        descendant = (
            "import signal,time,pathlib;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            + (f"pathlib.Path({ready!r}).write_text('ready');" if ready else "")
            + "time.sleep(120)"
        )
        child = subprocess.Popen([sys.executable, "-c", descendant])
        if ready:
            for _ in range(200):
                if os.path.exists(ready):
                    break
                time.sleep(0.05)
        if PIDFILE:
            with open(PIDFILE, "w", encoding="utf-8") as handle:
                handle.write(str(child.pid))
        init_frame()
        time.sleep(120)
        return 0

    if MODE == "no_init":
        emit({"event": "result", "usage": {"input_tokens": 1}})
        return 0

    if MODE == "deeply_nested":
        init_frame()
        sys.stdout.write("[" * 200_000 + "\n")
        sys.stdout.flush()
        return 0

    if MODE == "leader_exits_descendant_ignores":
        # The leader is responsive (default SIGTERM handler) but leaves behind a
        # descendant that ignores SIGTERM: the documented behaviour is to report
        # the survivor, never to chase the reused group id. The descendant signals
        # readiness so the case is deterministic (SIG_IGN installed first).
        ready = os.environ.get("FAKE_AGY_READYFILE")
        descendant = (
            "import signal,time,pathlib;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            f"pathlib.Path({ready!r}).write_text('ready');"
            "time.sleep(60)"
        )
        child = subprocess.Popen([sys.executable, "-c", descendant])
        if ready:
            for _ in range(200):
                if os.path.exists(ready):
                    break
                time.sleep(0.05)
        if PIDFILE:
            with open(PIDFILE, "w", encoding="utf-8") as handle:
                handle.write(str(child.pid))
        init_frame()
        time.sleep(60)
        return 0

    if MODE == "hang":
        init_frame()
        time.sleep(120)
        return 0

    if MODE == "partial_frame":
        # A kill can truncate a frame mid-write: the stream then ends with an
        # unterminated line, which must not be blamed on the provider.
        init_frame()
        sys.stdout.write('{"event":"step_update","agent_response":{"text_delta":"trunc')
        sys.stdout.flush()
        time.sleep(120)
        return 0

    if MODE == "malformed":
        init_frame()
        sys.stdout.write("{not valid json\n")
        sys.stdout.flush()
        return 0

    if MODE == "oversize":
        init_frame()
        sys.stdout.write('{"event":"step_update","blob":"' + "x" * 200_000 + '"}\n')
        sys.stdout.flush()
        return 0

    if MODE == "unknown_event":
        init_frame()
        emit({"event": "totally_new_thing", "x": 1})
        return 0

    if MODE == "no_result":
        init_frame()
        emit({"event": "step_update", "agent_response": {"text_delta": "partial text"}})
        return 0

    if MODE == "planning_only":
        init_frame()
        emit({"event": "step_update", "update": {"type": "thought", "text": PLANNING_TEXT}})
        emit({"event": "step_update", "checkpoint": {"text": CHECKPOINT_TEXT}})
        emit({"event": "result", "response": {"text": PLANNING_TEXT}, "usage": {"input_tokens": 5}})
        return 0

    if MODE == "stderr_noise":
        sys.stderr.write(STDERR_TOKEN + "\n")
        sys.stderr.flush()
        init_frame()
        emit({"event": "step_update", "agent_response": {"text_delta": "hello "}})
        emit({"event": "result"})
        return 0

    if MODE == "planning_delta":
        # A planning step that carries its own text_delta field: it must never be
        # forwarded as answer text (only agent_response.text_delta is).
        init_frame()
        emit(
            {
                "event": "step_update",
                "update": {"type": "thought", "text_delta": PLANNING_TEXT},
            }
        )
        emit({"event": "result"})
        return 0

    if MODE == "denied":
        init_frame()
        emit({"event": "step_update", "update": {"type": "tool_call", "id": "t1", "name": "shell"}})
        emit({"event": "step_update", "update": {"type": "permission_denied", "id": "t1"}})
        emit({"event": "result"})
        return 0

    if MODE == "error_result":
        init_frame()
        emit({"event": "result", "is_error": True})
        return 0

    # default: a successful turn with planning, tool and answer content
    init_frame()
    emit({"event": "step_update", "update": {"type": "thought", "text": PLANNING_TEXT}})
    emit({"event": "step_update", "agent_response": {"text_delta": "hello "}})
    emit({"event": "step_update", "update": {"type": "tool_call", "id": "t1", "name": "shell"}})
    emit({"event": "step_update", "update": {"type": "tool_result", "id": "t1", "status": "completed"}})
    emit({"event": "step_update", "agent_response": {"text_delta": "world"}})
    emit({"event": "result", "usage": {"input_tokens": 11, "output_tokens": 2}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
