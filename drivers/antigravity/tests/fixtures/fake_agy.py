"""Synthetic ``agy`` CLI for Antigravity driver tests.

Implements the documented ``agy --input-format stream-json --output-format
stream-json`` conversation protocol plus ``agy --version`` and the
authenticated ``agy models`` catalog listing. No real CLI or credential is
involved: the emitted stream shape is fixed to the official headless-docs
schema (``init`` / ``step_update`` / ``result`` envelopes with the payload
nested under a key matching the event name) and otherwise controlled by
environment variables:

  FAKE_AGY_MODE      session behavior (see MODE comments below)
  FAKE_AGY_CATALOG   ``agy models`` catalog behavior
  FAKE_AGY_VERSION   version string printed for ``--version`` (default 1.2.7)
  FAKE_AGY_LOG       optional jsonl path; records {"event": "start", ...} at
                     spawn and {"event": "prompt", ...} when a user frame is
                     read from stdin, so tests can prove ordering.
  FAKE_AGY_PIDFILE   writes the process-group leader pid for kill tests
  FAKE_AGY_READYFILE touched after the descendant spawn delay
"""

import json
import os
import subprocess
import sys
import time

MODE = os.environ.get("FAKE_AGY_MODE", "ok")
CATALOG = os.environ.get("FAKE_AGY_CATALOG", "ok")
VERSION = os.environ.get("FAKE_AGY_VERSION", "1.2.7")
LOG = os.environ.get("FAKE_AGY_LOG")
PIDFILE = os.environ.get("FAKE_AGY_PIDFILE")
READYFILE = os.environ.get("FAKE_AGY_READYFILE")
CONVERSATION = "fake-conversation"

# Exact catalog the parent's authenticated observation returned (plus decoys
# that share a prefix/suffix with a trusted ID: catalog parsing must compare
# whole IDs only).
DEFAULT_CATALOG = [
    ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
    ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
    ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("gemini-3.8-flash-high-x", "Decoy Suffix"),
    ("xgemini-3.8-flash-high", "Decoy Prefix"),
]


def log(record):
    if not LOG:
        return
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError:
        pass


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def touch(path):
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("ready")
    except OSError:
        pass


def write_pidfile():
    if PIDFILE:
        try:
            with open(PIDFILE, "w", encoding="utf-8") as handle:
                handle.write(str(os.getpid()))
        except OSError:
            pass


def step(step_index, state, step_type, **fields):
    """Emit one official ``step_update`` envelope (payload nested)."""
    payload = {
        "conversation_id": CONVERSATION,
        "step_index": step_index,
        "state": state,
        "step_type": step_type,
    }
    payload.update(fields)
    emit({"event": "step_update", "step_update": payload})


def result(status="SUCCESS", **fields):
    """Emit one official ``result`` envelope (payload nested)."""
    payload = {
        "conversation_id": CONVERSATION,
        "duration_seconds": 0.01,
        "num_turns": 1,
        "status": status,
    }
    payload.update(fields)
    emit({"event": "result", "result": payload})


def emit_init(model, permission_mode):
    init = {
        "cwd": os.getcwd(),
        "tools": ["run_command", "write_to_file", "ask_permission"],
        "permission_mode": permission_mode,
        "model": model,
    }
    if MODE == "missing_model":
        del init["model"]
    elif MODE == "wrong_model":
        init["model"] = "some-other-model"
    if MODE == "missing_perm":
        del init["permission_mode"]
    elif MODE == "wrong_permission":
        # Reports the opposite of what the spawn flags imply.
        init["permission_mode"] = (
            "request-review" if permission_mode == "always-proceed" else "always-proceed"
        )
    if MODE == "wrong_cwd":
        init["cwd"] = "/"
    emit({"event": "init", "conversation_id": CONVERSATION, "init": init})


def run_catalog():
    """``agy models``: preamble then tab-separated ``<id>\\t<label>`` rows."""
    if CATALOG == "hang":
        time.sleep(120)
        return 0
    if CATALOG == "nonzero":
        sys.stderr.write("error: authentication required\n")
        return 1
    if CATALOG == "malformed":
        sys.stdout.write('{"this is": "not the catalog format"}\n')
        sys.stdout.write("garbage without separators\n")
        sys.stdout.flush()
        return 0
    sys.stdout.write("Fetching available models...\n")
    if CATALOG == "missing":
        rows = [row for row in DEFAULT_CATALOG if row[0] != "gemini-3.8-flash-high"]
    else:
        rows = DEFAULT_CATALOG
    for model_id, label in rows:
        sys.stdout.write(f"{model_id}\t{label}\n")
    sys.stdout.flush()
    return 0


def read_prompt():
    line = sys.stdin.readline()
    if line:
        log({"event": "prompt", "line": line.strip()})
    return line


def run_session(argv):
    model = None
    skip_permissions = "--dangerously-skip-permissions" in argv
    if "--model" in argv:
        index = argv.index("--model")
        if index + 1 < len(argv):
            model = argv[index + 1]

    if MODE == "ignore_term":
        import signal

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if MODE == "leader_exits_descendant_ignores":
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"open({READYFILE!r},'w').write('ready')\n"
                "time.sleep(120)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        write_pidfile()
        time.sleep(0.4)
        return 0

    write_pidfile()

    if MODE == "no_init":
        # A frame stream that never opens with a valid init envelope.
        result("SUCCESS", response="premature")
        return 0
    if MODE == "never_init":
        # Session opens but no init ever arrives: the driver must bound the
        # handshake instead of consuming the whole deadline.
        time.sleep(120)
        return 0

    emit_init(model, "always-proceed" if skip_permissions else "request-review")

    if MODE in ("hang", "ignore_term", "hang_ignore_term"):
        read_prompt()
        time.sleep(120)
        return 0
    if MODE == "partial_frame":
        read_prompt()
        sys.stdout.write('{"event":"step_update","step_update":{"step_type":"agent_')
        sys.stdout.flush()
        time.sleep(120)
        return 0

    read_prompt()

    if MODE == "ok":
        step(0, "DONE", "user_input")
        step(1, "DONE", "checkpoint")
        step(2, "ACTIVE", "agent_response", text_delta="hello ")
        step(
            3,
            "DONE",
            "tool",
            tool_name="run_command",
            tool_info={
                "name": "run_command",
                "parameters": {"CommandLine": "echo hi"},
                "output": "hi\n",
            },
        )
        step(2, "DONE", "agent_response", text_delta="world")
        result(
            "SUCCESS",
            response="hello world",
            usage={
                "credits_used": 1,
                "input_tokens": 11,
                "other_tokens": 3,
                "output_tokens": 2,
                "total_tokens": 16,
            },
        )
        return 0
    if MODE == "usage_absent":
        step(0, "DONE", "agent_response", text_delta="hi")
        result("SUCCESS", response="hi")
        return 0
    if MODE == "usage_zero":
        step(0, "DONE", "agent_response", text_delta="hi")
        result(
            "SUCCESS",
            response="hi",
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        return 0
    if MODE == "planning_delta":
        # text_delta attached to a non-agent_response step must not leak.
        step(0, "DONE", "user_input")
        step(1, "ACTIVE", "thought", text_delta="SECRET-PLAN")
        step(1, "DONE", "thought", text="SECRET-PLAN")
        step(2, "DONE", "agent_response", text_delta="visible answer")
        result("SUCCESS", response="visible answer")
        return 0
    if MODE == "denied":
        # Soft tool denial: still a SUCCESS result but never a full success.
        step(0, "DONE", "user_input")
        step(
            1,
            "ACTIVE",
            "tool",
            tool_name="run_command",
            tool_info={
                "name": "run_command",
                "parameters": {"CommandLine": "rm -rf /"},
            },
        )
        step(
            1,
            "DONE",
            "tool",
            tool_name="run_command",
            tool_info={
                "name": "run_command",
                "error": {"type": "permission_denied", "message": "denied by policy"},
            },
        )
        result("SUCCESS", response="", usage={"input_tokens": 4, "output_tokens": 0})
        return 0
    if MODE == "stderr_noise":
        sys.stderr.write("token=sekrit\n")
        step(0, "DONE", "agent_response", text_delta="hello")
        result("SUCCESS", response="hello")
        return 0
    if MODE == "malformed":
        sys.stdout.write('{"event":"step_update","step_update":{"step_type":')
        sys.stdout.write(" not json}\n")
        sys.stdout.flush()
        return 0
    if MODE == "oversize":
        step(0, "DONE", "user_input")
        sys.stdout.write('{"event":"step_update","step_update":{"pad":"' + "x" * 70000 + '"}}\n')
        sys.stdout.flush()
        result("SUCCESS", response="")
        return 0
    if MODE == "deeply_nested":
        sys.stdout.write("[" * 200000 + "]\n")
        sys.stdout.flush()
        return 0
    if MODE == "unknown_event":
        emit({"event": "totally_new_thing", "data": {"a": 1}})
        step(0, "DONE", "agent_response", text_delta="hi")
        result("SUCCESS", response="hi")
        return 0
    if MODE == "no_result":
        step(0, "DONE", "agent_response", text_delta="hi")
        return 0
    if MODE == "error_result":
        step(0, "DONE", "agent_response", text_delta="partial")
        result("ERROR", response="")
        return 0
    if MODE == "canceled_result":
        result("CANCELED", response="")
        return 0
    if MODE == "unknown_status":
        result("MYSTERY", response="")
        return 0
    if MODE == "missing_status":
        emit({"event": "result", "result": {"conversation_id": CONVERSATION}})
        return 0
    # default: init then result
    step(0, "DONE", "agent_response", text_delta="hello")
    result("SUCCESS", response="hello")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    log(
        {
            "event": "start",
            "argv": argv,
            "cwd": os.getcwd(),
            "agy_env": {k: v for k, v in os.environ.items() if k.startswith("AGY_")},
        }
    )
    if "--version" in argv:
        if MODE == "bad_version":
            sys.stdout.write("not a version\n")
        else:
            sys.stdout.write(f"{VERSION}\n")
        return 0
    if "models" in argv:
        return run_catalog()
    if "--input-format" not in argv or "--output-format" not in argv:
        sys.stderr.write("unsupported invocation\n")
        return 2
    return run_session(argv)


if __name__ == "__main__":
    sys.exit(main())
