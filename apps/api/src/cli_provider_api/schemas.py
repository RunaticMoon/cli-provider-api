"""OpenAI-compatible request parsing plus strict unsupported-field rejection.

MVP is text messages only. Unsupported tools/tool_choice/images/sampling and any
unknown or execution-selecting field are rejected BEFORE the Runner is touched,
with structured classifications.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Mapping

from cli_provider_core import (
    BodyTimeout,
    BodyTooLarge,
    InvalidRequest,
    UnsupportedCapability,
)
from cli_provider_sdk import ID_PATTERN

import re

_ID = re.compile(ID_PATTERN)

ALLOWED_CHAT_FIELDS = frozenset({"model", "messages", "stream", "metadata"})

# Present but explicitly not implemented: report as unsupported capability.
UNSUPPORTED_CHAT_FIELDS = frozenset(
    {
        "tools",
        "tool_choice",
        "functions",
        "function_call",
        "response_format",
        "temperature",
        "top_p",
        "top_k",
        "n",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "seed",
        "max_tokens",
        "max_completion_tokens",
        "audio",
        "modalities",
        "parallel_tool_calls",
        "prediction",
        "store",
        "reasoning_effort",
        "input",
        "instructions",
    }
)

# Operator config owns the task policy; it is never selectable by a request.
ALLOWED_METADATA_FIELDS = frozenset({"task_id", "workspace_id"})
ROLES = frozenset({"system", "user", "assistant"})

# A message is exactly {role, content} in this MVP. Execution-selecting keys
# (tools/functions/name) are reported as unsupported capability; any other key
# is an invalid request. Neither is ever silently dropped.
ALLOWED_MESSAGE_FIELDS = frozenset({"role", "content"})
UNSUPPORTED_MESSAGE_FIELDS = frozenset(
    {
        "tool_calls",
        "tool_call_id",
        "function_call",
        "functions",
        "name",
        "audio",
        "image_url",
        "refusal",
    }
)


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: list[dict[str, str]]
    stream: bool
    task_id: str
    workspace_id: str


async def read_bounded_json(
    request: Any, limit: int, *, timeout_seconds: float
) -> dict[str, Any]:
    """Read a size-bounded JSON object under one fixed whole-body deadline.

    The byte bound alone does not stop a slow drip feed, so the entire read is
    wrapped in a single timeout that is never renewed per chunk.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise BodyTooLarge("request body exceeds the configured limit")
        except ValueError as exc:
            raise InvalidRequest("invalid Content-Length header") from exc

    chunks: list[bytes] = []
    total = 0
    try:
        async with asyncio.timeout(timeout_seconds):
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    raise BodyTooLarge("request body exceeds the configured limit")
                chunks.append(chunk)
    except TimeoutError as exc:
        raise BodyTimeout("request body read timed out") from exc
    raw = b"".join(chunks)
    if not raw:
        raise InvalidRequest("request body is empty")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidRequest("request body is not valid JSON") from exc
    if not isinstance(data, dict):
        raise InvalidRequest("request body must be a JSON object")
    return data


def parse_chat_request(data: Mapping[str, Any]) -> ChatRequest:
    for field in data:
        if field in UNSUPPORTED_CHAT_FIELDS:
            raise UnsupportedCapability(
                f"{field!r} is not supported by this runtime; text messages only"
            )
        if field not in ALLOWED_CHAT_FIELDS:
            raise InvalidRequest(f"unknown request field {field!r}")

    model = data.get("model")
    if not isinstance(model, str) or not model:
        raise InvalidRequest("'model' is required and must be a string")

    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise InvalidRequest("'messages' is required and must be a non-empty array")

    messages: list[dict[str, str]] = []
    for index, message in enumerate(raw_messages):
        if not isinstance(message, dict):
            raise InvalidRequest(f"messages[{index}] must be an object")
        role = message.get("role")
        if role not in ROLES:
            raise UnsupportedCapability(
                f"messages[{index}].role {role!r} is not supported; "
                "system/user/assistant only"
            )
        content = message.get("content")
        if isinstance(content, (list, dict)):
            raise UnsupportedCapability(
                f"messages[{index}].content must be text; multimodal parts are "
                "not supported"
            )
        if not isinstance(content, str):
            raise InvalidRequest(f"messages[{index}].content must be a string")
        for key in message:
            if key in ALLOWED_MESSAGE_FIELDS:
                continue
            if key in UNSUPPORTED_MESSAGE_FIELDS:
                raise UnsupportedCapability(
                    f"messages[{index}].{key} is not supported; text messages "
                    "with role/content only"
                )
            raise InvalidRequest(f"unknown message field messages[{index}].{key!r}")
        messages.append({"role": str(role), "content": content})

    stream = data.get("stream", False)
    if not isinstance(stream, bool):
        raise InvalidRequest("'stream' must be a boolean")

    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        raise InvalidRequest("'metadata' is required and must be an object")
    for key in metadata:
        if key not in ALLOWED_METADATA_FIELDS:
            raise InvalidRequest(f"unknown metadata field {key!r}")
    task_id = metadata.get("task_id")
    workspace_id = metadata.get("workspace_id")
    if not isinstance(task_id, str) or _ID.match(task_id) is None:
        raise InvalidRequest("metadata.task_id is required and must be a valid id")
    if not isinstance(workspace_id, str) or _ID.match(workspace_id) is None:
        raise InvalidRequest("metadata.workspace_id is required and must be a valid id")

    return ChatRequest(
        model=model,
        messages=messages,
        stream=stream,
        task_id=task_id,
        workspace_id=workspace_id,
    )


def chat_completion(
    *,
    chat_id: str,
    model: str,
    created: int,
    content: str,
    run: dict[str, Any],
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": chat_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "run": run,
    }
    # Unknown usage is omitted/null, never zero. Reported/estimated usage keeps
    # its provenance; a count the driver did not supply stays null rather than
    # being invented as 0.
    if usage is not None and usage.get("provenance") != "unknown":
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        body["usage"] = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": (
                input_tokens + output_tokens
                if input_tokens is not None and output_tokens is not None
                else None
            ),
        }
    else:
        body["usage"] = None
    return body


def chat_chunk(
    *,
    chat_id: str,
    model: str,
    created: int,
    content: str | None,
    finish_reason: str | None,
    run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    chunk: dict[str, Any] = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    # Run identity/outcome metadata rides the JSON body (a 9Router-preserved
    # extension), never the answer text.
    if run is not None:
        chunk["run"] = run
    return chunk
