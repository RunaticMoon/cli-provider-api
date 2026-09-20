"""Revision+policy-keyed decision cache with mutation detection."""

from __future__ import annotations

import json

import pytest

from cli_provider_kanban.cache import CacheError, DecisionCache


def _decision():
    return {"recommended_action": "execute", "route": "worker.code.standard"}


def test_store_and_hit(tmp_path):
    cache = DecisionCache(tmp_path / "cache.json")
    cache.store("t_1", "1", "p1", "fp-a", _decision())
    entry = cache.lookup("t_1", "1", "p1")
    assert entry is not None
    assert entry.fingerprint == "fp-a"
    assert entry.decision == _decision()


def test_fingerprint_mismatch_detected(tmp_path):
    cache = DecisionCache(tmp_path / "cache.json")
    cache.store("t_1", "1", "p1", "fp-a", _decision())
    entry = cache.lookup("t_1", "1", "p1")
    assert entry is not None
    assert entry.fingerprint != "fp-b"  # caller treats as conflict


def test_different_revision_misses(tmp_path):
    cache = DecisionCache(tmp_path / "cache.json")
    cache.store("t_1", "1", "p1", "fp-a", _decision())
    assert cache.lookup("t_1", "2", "p1") is None


def test_different_policy_version_misses(tmp_path):
    cache = DecisionCache(tmp_path / "cache.json")
    cache.store("t_1", "1", "p1", "fp-a", _decision())
    assert cache.lookup("t_1", "1", "p2") is None


def test_persistence_roundtrip(tmp_path):
    path = tmp_path / "cache.json"
    cache = DecisionCache(path)
    cache.store("t_1", "1", "p1", "fp-a", _decision())
    cache.save()
    reloaded = DecisionCache(path)
    assert reloaded.lookup("t_1", "1", "p1") is not None


def test_corrupt_cache_fails_closed(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CacheError):
        DecisionCache(path)


def test_missing_cache_is_empty(tmp_path):
    cache = DecisionCache(tmp_path / "absent.json")
    assert cache.lookup("t_1", "1", "p1") is None


def test_restoring_same_key_is_idempotent(tmp_path):
    path = tmp_path / "cache.json"
    cache = DecisionCache(path)
    cache.store("t_1", "1", "p1", "fp-a", _decision())
    cache.store("t_1", "1", "p1", "fp-a", _decision())
    cache.save()
    raw = json.loads(path.read_text())
    entries = raw["entries"]
    matches = [e for e in entries if e["task_id"] == "t_1"]
    assert len(matches) == 1
