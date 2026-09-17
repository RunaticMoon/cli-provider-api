"""Opaque, URL-safe identifiers.

IDs are generated server-side and are never caller-controlled filesystem
paths. Artifact IDs are additionally validated against a strict pattern before
any path is derived from them.
"""

from __future__ import annotations

import re
import secrets

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


def new_token(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(18).replace('-', 'x').replace('_', 'y')}"


def new_run_id() -> str:
    return new_token("run_")


def new_attempt_id() -> str:
    return new_token("att_")


def new_artifact_id() -> str:
    return new_token("art_")


def chat_id_for_run(run_id: str) -> str:
    """Deterministic completion id bound to the actual run.

    A gateway may drop the initial metadata-only SSE chunk, so the standard
    completion ``id`` must let a caller identify the in-flight run. The exact
    documented format is ``chatcmpl-{run_id}`` (run ids already start ``run_``).
    """
    return f"chatcmpl-{run_id}"


def is_safe_id(value: str) -> bool:
    return bool(value) and _ID_RE.match(value) is not None
