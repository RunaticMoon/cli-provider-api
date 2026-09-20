#!/usr/bin/env python3
"""Loopback OpenAI-compatible chat-completions mock for the real-Hermes fixture.

Scripts a two-tool-call conversation (``read_file`` then ``write_file``) and a
final answer. Every request body is appended as one JSON line to the log file
named by ``HERMES_MOCK_LOG`` so tests can assert on the exact wire shape —
model pin, ``reasoning_effort``, tool-result history and the DeepSeek
``reasoning_content`` echo. Non-chat endpoints (the ``/api/show`` Ollama probe,
``GET /models``) are answered inertly; nothing here is a real account or
external network call.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("HERMES_MOCK_LOG", "/dev/null")


def log_request(path: str, body: dict) -> None:
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"_path": path, **body}) + "\n")


def sse_chunk(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def tool_call_msg(call_id: str, name: str, args: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    }


# Workspace-relative file names the fixture model "chooses"; the test asserts
# the real bytes land in the isolated workspace.
READ_PATH = "input.txt"
WRITE_PATH = "agent-out.txt"
WRITE_CONTENT = "fixture-agent-wrote-this"
ANSWER = "fixture final answer DONE"


def plan(body: dict) -> tuple[str, dict]:
    messages = body.get("messages", [])
    n_tool_results = sum(1 for m in messages if m.get("role") == "tool")
    if n_tool_results == 0:
        return "tool_calls", tool_call_msg("call_1", "read_file", {"path": READ_PATH})
    if n_tool_results == 1:
        return "tool_calls", tool_call_msg(
            "call_2", "write_file", {"path": WRITE_PATH, "content": WRITE_CONTENT})
    return "stop", {"role": "assistant", "content": ANSWER}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        return None

    def do_GET(self) -> None:
        log_request(
            self.path,
            {"_get": True, "_auth": bool(self.headers.get("Authorization"))},
        )
        if self.path.endswith("/models"):
            # The official catalog check: the two pinned exact ids are members.
            body = json.dumps({
                "object": "list",
                "data": [
                    {"id": "deepseek-v4.1-flash", "object": "model"},
                    {"id": "deepseek/deepseek-v4.1-flash", "object": "model"},
                ],
            }).encode()
        else:
            body = json.dumps({"object": "list", "data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except Exception:
            body = {"_unparsed": raw[:200].decode("utf-8", "replace")}
        log_request(self.path, body)
        if "messages" not in body:
            # Capability probes (e.g. Ollama-style /api/show) get a 404: the
            # client treats that as "not Ollama" and moves on.
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        finish, message = plan(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        delta: dict = {"role": "assistant"}
        if message.get("content"):
            delta["content"] = message["content"]
        if message.get("tool_calls"):
            delta["tool_calls"] = [
                {"index": i, "id": tc["id"], "type": "function",
                 "function": {"name": tc["function"]["name"],
                              "arguments": tc["function"]["arguments"]}}
                for i, tc in enumerate(message["tool_calls"])
            ]
        model = body.get("model", "mock")
        self.wfile.write(sse_chunk({
            "id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }))
        self.wfile.write(sse_chunk({
            "id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }))
        self.wfile.write(b"data: [DONE]\n\n")


def main() -> int:
    port = int(sys.argv[1])
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
