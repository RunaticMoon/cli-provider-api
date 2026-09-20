"""Decision cache: ``(task_id, task_revision, policy_version)`` -> decision.

Cache entries also carry the card fingerprint computed at classification time.
A lookup under the same key whose stored fingerprint differs from the card's
current fingerprint means the card was mutated without a revision bump — the
caller must reject rather than serve the stale decision.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import CacheError


@dataclass(frozen=True)
class CacheEntry:
    task_id: str
    task_revision: str
    policy_version: str
    fingerprint: str
    decision: dict


class DecisionCache:
    """JSON-file cache. Corrupt content fails closed with :class:`CacheError`."""

    def __init__(self, path: Path | str):
        self._path = Path(path)
        self._entries: list[CacheEntry] = []
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheError(f"decision cache {self._path} is unreadable: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
            raise CacheError(f"decision cache {self._path} has no entries list")
        for item in raw["entries"]:
            try:
                self._entries.append(
                    CacheEntry(
                        task_id=str(item["task_id"]),
                        task_revision=str(item["task_revision"]),
                        policy_version=str(item["policy_version"]),
                        fingerprint=str(item["fingerprint"]),
                        decision=dict(item["decision"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise CacheError(
                    f"decision cache {self._path} holds a malformed entry: {exc}"
                ) from exc

    def lookup(
        self, task_id: str, task_revision: str, policy_version: str
    ) -> CacheEntry | None:
        for entry in self._entries:
            if (
                entry.task_id == task_id
                and entry.task_revision == task_revision
                and entry.policy_version == policy_version
            ):
                return entry
        return None

    def store(
        self,
        task_id: str,
        task_revision: str,
        policy_version: str,
        fingerprint: str,
        decision: dict,
    ) -> None:
        existing = self.lookup(task_id, task_revision, policy_version)
        if existing is not None:
            if existing.fingerprint == fingerprint and existing.decision == decision:
                return  # idempotent re-store
            raise CacheError(
                f"cache conflict for {task_id} rev {task_revision}: a different "
                "fingerprint/decision is already stored — bump task_revision"
            )
        self._entries.append(
            CacheEntry(task_id, task_revision, policy_version, fingerprint, decision)
        )

    def save(self) -> None:
        """Atomic write (temp + rename) with 0600 permissions."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "entries": [
                {
                    "task_id": e.task_id,
                    "task_revision": e.task_revision,
                    "policy_version": e.policy_version,
                    "fingerprint": e.fingerprint,
                    "decision": e.decision,
                }
                for e in self._entries
            ],
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=self._path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
