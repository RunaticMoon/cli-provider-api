"""TaskSpec extraction: structured card body or the local Lead task map.

Two sanctioned sources, in order:

1. the card body — either the whole body is a JSON object, or it contains
   exactly one fenced block tagged ``jev-task-spec`` carrying the JSON spec;
2. a local Lead-maintained mapping file (``policy.task_map``) keyed by task id.

Anything else — no contract, malformed JSON, schema violations, a spec bound
to a different task id — is reported through ``SpecResult.errors`` and the
classifier turns it into ``replan``; the shadow never guesses.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from .models import TaskSpec

SPEC_BLOCK_LANG = "jev-task-spec"

_BLOCK_RE = re.compile(
    r"```{lang}\s*\n(.*?)\n\s*```".format(lang=re.escape(SPEC_BLOCK_LANG)),
    re.DOTALL,
)


@dataclass(frozen=True)
class SpecResult:
    spec: TaskSpec | None
    source: Literal["body", "task_map", "none"]
    errors: tuple[str, ...] = field(default_factory=tuple)


def _validation_errors(exc: ValidationError) -> list[str]:
    """Compact, value-free error strings (never echo card content)."""
    out = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
        out.append(f"{loc}: {err.get('msg', 'invalid')}")
    return out


def _parse_spec_json(raw: str, *, task_id: str) -> SpecResult:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return SpecResult(
            None, "body", (f"body: spec block is not valid JSON ({exc.msg})",),
        )
    if not isinstance(data, dict):
        return SpecResult(None, "body", ("body: spec must be a JSON object",))
    if data.get("task_id") != task_id:
        return SpecResult(
            None, "body",
            (f"body: spec task_id {data.get('task_id')!r} does not match "
             f"card {task_id!r}",),
        )
    try:
        return SpecResult(TaskSpec.model_validate(data), "body")
    except ValidationError as exc:
        return SpecResult(None, "body", tuple(_validation_errors(exc)))


def _from_task_map(task_id: str, task_map: dict | None) -> SpecResult:
    if not task_map or task_id not in task_map:
        return SpecResult(None, "none")
    entry = task_map[task_id]
    if not isinstance(entry, dict):
        return SpecResult(None, "task_map", ("task_map entry must be an object",))
    data = dict(entry)
    declared = data.get("task_id")
    if declared is not None and declared != task_id:
        return SpecResult(
            None, "task_map",
            (f"task_map entry id {declared!r} does not match card {task_id!r}",),
        )
    data["task_id"] = task_id
    try:
        return SpecResult(TaskSpec.model_validate(data), "task_map")
    except ValidationError as exc:
        return SpecResult(None, "task_map", tuple(_validation_errors(exc)))


def resolve_spec(
    task,
    task_map: dict | None,
    *,
    max_body_bytes: int = 65_536,
    max_spec_bytes: int = 32_768,
) -> SpecResult:
    """Resolve the TaskSpec for a board row; never raises on card content."""
    body = task.body
    if body is None or not body.strip():
        return _from_task_map(task.id, task_map)
    if len(body.encode("utf-8", errors="replace")) > max_body_bytes:
        return SpecResult(
            None, "none",
            (f"card body exceeds {max_body_bytes} bytes",),
        )
    blocks = _BLOCK_RE.findall(body)
    if len(blocks) > 1:
        return SpecResult(
            None, "body", ("card carries multiple jev-task-spec blocks",)
        )
    if blocks:
        block = blocks[0]
        if len(block.encode("utf-8", errors="replace")) > max_spec_bytes:
            return SpecResult(
                None, "body",
                (f"spec block exceeds {max_spec_bytes} bytes",),
            )
        return _parse_spec_json(block, task_id=task.id)
    stripped = body.strip()
    if stripped.startswith("{"):
        return _parse_spec_json(stripped, task_id=task.id)
    return _from_task_map(task.id, task_map)
