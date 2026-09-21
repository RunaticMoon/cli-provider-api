#!/usr/bin/env python3
"""Synthetic ``devin`` CLI stand-in for driver tests.

Speaks newline-delimited JSON-RPC 2.0 (ACP shape) on stdio for ``acp`` and a
plain JSON document for ``models list --format json``. Behaviour is selected by
the operator-controlled ``FAKE_DEVIN_MODE``/``FAKE_DEVIN_CATALOG`` environment
variables - never by a run request. No real CLI, account or network call
happens here; every frame is a fixture. Inbound client messages and the
inherited-environment check are appended to ``FAKE_DEVIN_LOG`` as JSON lines so
tests can assert what the driver actually sent.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

MODE = os.environ.get("FAKE_DEVIN_MODE", "ok")
CATALOG = os.environ.get("FAKE_DEVIN_CATALOG", "ok")
LOG = os.environ.get("FAKE_DEVIN_LOG")

# The exact id the driver pinned on this process's argv (--model <id>).
_argv = sys.argv[1:]
REQUEST_MODEL = (
    _argv[_argv.index("--model") + 1] if "--model" in _argv else "swe-2-max"
)

SECRET_THOUGHT = "SECRET-THOUGHT-TEXT"
SECRET_STDERR = "SECRET-STDERR-TOKEN"
SECRET_RPC_ERROR = "SECRET-RPC-ERROR-TEXT"

SANITIZED_ENV = ("DEVIN_REFUSAL_FALLBACK", "DEVIN_MODEL", "DEVIN_PERMISSION_MODE")

MODES = [
    {"id": "accept-edits", "name": "Code"},
    {"id": "smart", "name": "Smart"},
    {"id": "ask", "name": "Ask"},
    {"id": "plan", "name": "Plan"},
    {"id": "bypass", "name": "Bypass Permissions"},
]

GOOD_CATALOG = {
    "families": [
        {
            "family_label": "SWE-2",
            "family_uid": "swe-2",
            "slug": "swe-2",
            "aliases": ["swe"],
            "variants": [
                {
                    "model_uid": "swe-2-high",
                    "label": "SWE-2 High",
                    "cost_tier": "Free",
                },
                {
                    "model_uid": "swe-2-medium",
                    "label": "SWE-2 Medium",
                    "cost_tier": "Free",
                },
                {
                    "model_uid": "swe-2-max",
                    "label": "SWE-2 Max",
                    "cost_tier": "Free",
                },
            ],
        }
    ]
}


def log_record(record: dict) -> None:
    if not LOG:
        return
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def respond(request_id, result) -> None:
    emit({"jsonrpc": "2.0", "id": request_id, "result": result})


def respond_error(request_id, code: int, message: str) -> None:
    emit({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def notify_update(session_id: str, update: dict) -> None:
    emit(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        }
    )


def catalog_document() -> dict:
    if CATALOG == "missing":
        return {"families": [{"family_uid": "swe-2", "variants": [{"model_uid": "swe-2-high", "cost_tier": "Free"}]}]}
    if CATALOG == "cost":
        doc = json.loads(json.dumps(GOOD_CATALOG))
        for variant in doc["families"][0]["variants"]:
            if variant["model_uid"] == "swe-2-max":
                variant["cost_tier"] = "High cost"
                variant["cost_summary"] = "$5 / 1M Input"
        return doc
    if CATALOG == "empty":
        return {"families": []}
    return GOOD_CATALOG


def session_new_result() -> dict:
    current_mode = "plan" if MODE == "current_mode_plan" else "accept-edits"
    # The fake acknowledges exactly the model pinned on the argv, like the real
    # ACP `model` configOption echoes the session's effective model.
    acknowledged = "swe-2-medium" if MODE == "model_mismatch" else REQUEST_MODEL
    result = {
        "sessionId": "fixture-session-1",
        "modes": {"currentModeId": current_mode, "availableModes": MODES},
        "configOptions": [
            {
                "id": "mode",
                "name": "Session Mode",
                "category": "mode",
                "type": "select",
                "currentValue": current_mode,
            },
            {
                "id": "model",
                "name": "Model",
                "description": "AI model to use",
                "category": "model",
                "type": "select",
                "currentValue": acknowledged,
            },
        ],
        "_meta": {"cognition.ai/isLocked": False},
    }
    if MODE == "no_config_options":
        result.pop("configOptions")
    if MODE == "no_modes":
        result.pop("modes")
    return result


def run_prompt(params: dict) -> None:
    """Emit the turn traffic for the prompt, then the prompt response."""
    session_id = params.get("sessionId", "fixture-session-1")

    if MODE == "eof_mid_prompt":
        notify_update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "partial answer"},
            },
        )
        # The CLI died mid-turn: close stdout and exit without a response.
        sys.stdout.close()
        raise SystemExit(0)

    if MODE == "malformed":
        sys.stdout.write("{not valid json\n")
        sys.stdout.flush()
        raise SystemExit(0)

    if MODE == "oversize":
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session_id, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "x" * 200_000}}}})
        raise SystemExit(0)

    if MODE == "hang":
        # Never answer the prompt; the cancel test drives session/cancel.
        return

    if MODE == "unknown_stop":
        respond(PENDING_PROMPT_ID[0], {"stopReason": "totally_new_reason"})
        PENDING_PROMPT_ID[0] = None
        return

    if MODE == "rpc_error":
        respond_error(PENDING_PROMPT_ID[0], -32000, SECRET_RPC_ERROR)
        PENDING_PROMPT_ID[0] = None
        return

    if MODE == "thought_only":
        notify_update(
            session_id,
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": SECRET_THOUGHT},
            },
        )
        respond(PENDING_PROMPT_ID[0], {"stopReason": "end_turn"})
        PENDING_PROMPT_ID[0] = None
        return

    # default "ok" turn: thought + tool + answer chunks + usage update
    notify_update(
        session_id,
        {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": SECRET_THOUGHT},
        },
    )
    notify_update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-1",
            "title": "Editing file",
            "kind": "edit",
            "status": "in_progress",
        },
    )
    notify_update(
        session_id,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "status": "completed",
            "content": [
                {"type": "diff", "path": "/workspace/app.py", "newText": "x = 1\n"}
            ],
        },
    )
    notify_update(
        session_id,
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "Hello "},
        },
    )
    notify_update(
        session_id,
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "world"},
        },
    )
    notify_update(
        session_id,
        {"sessionUpdate": "usage_update", "used": 53000, "size": 262000},
    )
    if MODE == "max_tokens_stop":
        respond(PENDING_PROMPT_ID[0], {"stopReason": "max_tokens"})
    elif MODE == "refusal":
        respond(PENDING_PROMPT_ID[0], {"stopReason": "refusal"})
    else:
        respond(PENDING_PROMPT_ID[0], {"stopReason": "end_turn"})
    PENDING_PROMPT_ID[0] = None


PENDING_PROMPT_ID: list = [None]
PENDING_PERMISSION_ID = "fixture-perm-1"


def acp_main() -> int:
    log_record(
        {
            "event": "acp_start",
            "argv": sys.argv[1:],
            "env": {name: ("present" if name in os.environ else "absent") for name in SANITIZED_ENV},
        }
    )
    if MODE == "stderr_noise":
        sys.stderr.write(SECRET_STDERR + "\n")
        sys.stderr.flush()
    permission_requested = False

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        if method:
            log_record({"received": method, "id": message.get("id")})
        else:
            log_record({"received": "response", "id": message.get("id"), "keys": sorted(message.keys())})

        if method == "initialize":
            if MODE == "init_error":
                respond_error(message["id"], -32603, "init exploded")
            elif MODE == "init_bad_protocol":
                respond(
                    message["id"],
                    {
                        "protocolVersion": 99,
                        "agentCapabilities": {},
                        "agentInfo": {"name": "affogato", "version": "0.0.0-dev"},
                    },
                )
            else:
                respond(
                    message["id"],
                    {
                        "protocolVersion": 1,
                        "agentCapabilities": {
                            "loadSession": True,
                            "promptCapabilities": {"image": True, "embeddedContext": True},
                        },
                        "authMethods": [{"id": "devin-browser", "name": "Log in with browser"}],
                        "agentInfo": {"name": "affogato", "version": "0.0.0-dev"},
                        "_meta": {"mcpConfigPath": "/fixture/mcp_config.json"},
                    },
                )
        elif method == "session/new":
            if MODE == "new_error":
                respond_error(message["id"], -32603, "session refused")
            else:
                respond(message["id"], session_new_result())
        elif method == "session/set_mode":
            requested = message.get("params", {}).get("modeId")
            respond(message["id"], {})
            if MODE == "mode_no_ack":
                continue
            ack_mode = "ask" if MODE == "mode_mismatch" else requested
            notify_update(
                message.get("params", {}).get("sessionId", "fixture-session-1"),
                {"sessionUpdate": "current_mode_update", "currentModeId": ack_mode},
            )
        elif method == "session/prompt":
            PENDING_PROMPT_ID[0] = message["id"]
            blocks = message.get("params", {}).get("prompt", [])
            log_record(
                {
                    "received": "session/prompt:begin",
                    "prompt_text": "".join(
                        block.get("text", "") for block in blocks if isinstance(block, dict)
                    )[:400],
                }
            )
            if MODE == "permission" and not permission_requested:
                permission_requested = True
                session_id = message.get("params", {}).get("sessionId", "fixture-session-1")
                notify_update(
                    session_id,
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-perm",
                        "title": "Running command",
                        "kind": "execute",
                        "status": "pending",
                    },
                )
                emit(
                    {
                        "jsonrpc": "2.0",
                        "id": PENDING_PERMISSION_ID,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": session_id,
                            "toolCall": {"toolCallId": "tc-perm", "title": "Running command"},
                            "options": [
                                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                                {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                            ],
                        },
                    }
                )
                continue
            run_prompt(message.get("params", {}))
        elif method == "session/cancel":
            # Notification: no response. Answer the pending prompt as cancelled.
            if PENDING_PROMPT_ID[0] is not None:
                respond(PENDING_PROMPT_ID[0], {"stopReason": "cancelled"})
                PENDING_PROMPT_ID[0] = None
            if MODE == "hang_on_cancel":
                time.sleep(60)
        elif method is None and message.get("id") == PENDING_PERMISSION_ID:
            # Response to our permission request.
            outcome = message.get("result", {}).get("outcome", {})
            log_record({"permission_outcome": outcome})
            session_id = "fixture-session-1"
            notify_update(
                session_id,
                {"sessionUpdate": "tool_call_update", "toolCallId": "tc-perm", "status": "failed"},
            )
            notify_update(
                session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "denied path taken"},
                },
            )
            respond(PENDING_PROMPT_ID[0], {"stopReason": "end_turn"})
            PENDING_PROMPT_ID[0] = None
        else:
            log_record({"unhandled": message})
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        if MODE == "bad_version":
            print("not-a-version")
        else:
            print("devin 3000.10.31 (b98cc431)")
        return 0

    if argv[:3] == ["models", "list", "--format"] and len(argv) > 3 and argv[3] == "json":
        if CATALOG == "bad_json":
            print("{not json at all")
        else:
            print(json.dumps(catalog_document()))
        return 0

    if "acp" in argv:
        if MODE == "ignore_term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        return acp_main()

    sys.stderr.write("fixture: unsupported argv\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
