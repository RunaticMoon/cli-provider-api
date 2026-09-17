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
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def hash_api_key(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def verify_api_key(key: str, expected_hash: str) -> bool:
    candidate = hash_api_key(key)
    return hmac.compare_digest(candidate, expected_hash)
