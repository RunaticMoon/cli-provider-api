"""Logical request hashing and API-key hashing.

The logical request hash deliberately excludes the selected provider/model/preset
so a future pre-execution fallback can reuse the same logical attempt. It includes
caller, task, workspace, messages and task policy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping, Sequence


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_hash(
    *,
    principal: str,
    task_id: str,
    workspace_id: str,
    task_policy: str,
    messages: Sequence[Mapping[str, Any]],
    execution: Mapping[str, Any] | None = None,
    reasoning_effort: str | None = None,
) -> str:
    payload = {
        "principal": principal,
        "task_id": task_id,
        "workspace_id": workspace_id,
        "task_policy": task_policy,
        "messages": [
            {"role": m.get("role"), "content": m.get("content")} for m in messages
        ],
    }
    # The caller-supplied execution context is part of the logical request: a
    # replay carrying different context is a content conflict, while a request
    # without it keeps the legacy digest unchanged. Binding it is what holds
    # the task/revision/base/route/policy context fixed across all candidates
    # of one task_id — a changed route/policy is not a fallback, it needs a
    # new Lead revision decision.
    if execution is not None:
        payload["execution"] = dict(execution)
    # An explicit reasoning-effort request is part of the logical request too:
    # replaying the same task under a different effort is a content conflict,
    # never a silent cached answer from a differently-efforted run. When the
    # field is absent the digest is identical to the legacy shape, preserving
    # the pre-effort cross-provider retry semantics.
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def hash_api_key(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def verify_api_key(key: str, expected_hash: str) -> bool:
    candidate = hash_api_key(key)
    return hmac.compare_digest(candidate, expected_hash)
