"""Tests for manager/categorize.py — consolidation caching in _normalize_categories."""
import json
from unittest.mock import call, patch

import pytest

from manager.categorize import _normalize_categories


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_cache(roots: list[str]) -> tuple[dict, set[str]]:
    """Build a merchant cache and name set with one merchant per root category.

    Each merchant gets a 2-level path: [root, "Sub"].  With 16+ distinct roots
    the taxonomy exceeds the 15-root threshold and triggers consolidation.
    """
    cache: dict = {}
    names: set[str] = set()
    for root in roots:
        name = f"Merchant_{root}"
        cache[name] = {"path": [root, "Sub"]}
        names.add(name)
    return cache, names


def _many_roots(n: int = 16) -> list[str]:
    return [f"Root{i:02d}" for i in range(n)]


# ── Below-threshold: no LLM, no cache I/O ─────────────────────────────────────

def test_normalize_skips_when_below_threshold():
    """With ≤15 root categories and ≤100 paths, normalisation is a no-op."""
    roots = _many_roots(14)
    cache, names = _make_cache(roots)

    with patch("manager.categorize.sheets_cache.read_cache") as mock_read, \
         patch("manager.categorize.sheets_cache.write_cache") as mock_write, \
         patch("manager.categorize.llm_structured") as mock_llm:
        _normalize_categories(cache, names)

    mock_llm.assert_not_called()
    mock_read.assert_not_called()
    mock_write.assert_not_called()


def test_normalize_triggers_at_16_roots():
    """With 16 root categories, normalisation should attempt LLM or cache lookup."""
    roots = _many_roots(16)
    cache, names = _make_cache(roots)

    with patch("manager.categorize.sheets_cache.read_cache", return_value={}), \
         patch("manager.categorize.sheets_cache.write_cache"), \
         patch("manager.categorize.llm_structured",
               return_value={"mapping": {str(i): ["Consolidated", f"Root{i:02d}"]
                                         for i in range(len(roots))}}):
        _normalize_categories(cache, names)  # should not raise


# ── LLM called on cache miss ───────────────────────────────────────────────────

def test_normalize_calls_llm_on_cache_miss():
    """When no cached mapping exists for this path-set, LLM must be called."""
    roots = _many_roots(16)
    cache, names = _make_cache(roots)

    llm_result = {"mapping": {str(i): ["Merged"] for i in range(len(roots))}}

    with patch("manager.categorize.sheets_cache.read_cache", return_value={}), \
         patch("manager.categorize.sheets_cache.write_cache"), \
         patch("manager.categorize.llm_structured", return_value=llm_result) as mock_llm:
        _normalize_categories(cache, names)

    mock_llm.assert_called_once()


def test_normalize_stores_mapping_in_consolidation_cache():
    """After calling LLM, the result must be persisted to the consolidation cache tab."""
    roots = _many_roots(16)
    cache, names = _make_cache(roots)

    llm_result = {"mapping": {str(i): ["Merged", f"Root{i:02d}"] for i in range(len(roots))}}
    written: dict = {}

    def fake_write(tab, data):
        written[tab] = data

    with patch("manager.categorize.sheets_cache.read_cache", return_value={}), \
         patch("manager.categorize.sheets_cache.write_cache", side_effect=fake_write), \
         patch("manager.categorize.llm_structured", return_value=llm_result):
        _normalize_categories(cache, names)

    assert "_consolidation_cache" in written
    # Stored entry should be a dict keyed by json-encoded path strings
    stored = written["_consolidation_cache"]
    assert len(stored) == 1  # one hash entry
    mapping_blob = next(iter(stored.values()))
    assert isinstance(mapping_blob, dict)


def test_normalize_applies_llm_mapping_to_merchant_cache():
    """LLM-returned paths must be written into the merchant cache entries."""
    cache = {
        "Merchant_A": {"path": ["Root00", "Sub"]},
        "Merchant_B": {"path": ["Root01", "Sub"]},
    }
    # Add enough extra roots so the threshold is hit
    for i in range(2, 16):
        cache[f"Merchant_{i:02d}"] = {"path": [f"Root{i:02d}", "Sub"]}

    names = set(cache.keys())
    paths_list = sorted({tuple(v["path"]) for v in cache.values()})
    new_path = ["Shopping", "General"]
    llm_result = {"mapping": {str(i): new_path for i in range(len(paths_list))}}

    with patch("manager.categorize.sheets_cache.read_cache", return_value={}), \
         patch("manager.categorize.sheets_cache.write_cache"), \
         patch("manager.categorize.llm_structured", return_value=llm_result):
        _normalize_categories(cache, names)

    for entry in cache.values():
        assert entry["path"] == new_path


def test_normalize_sets_old_path_on_first_change():
    """When a merchant's path is changed, old_path should be preserved."""
    cache = {}
    for i in range(16):
        cache[f"M{i:02d}"] = {"path": [f"Root{i:02d}", "Sub"]}
    names = set(cache.keys())
    original = dict(cache["M00"]["path"])

    paths_list = sorted({tuple(v["path"]) for v in cache.values()})
    new_path = ["NewCat"]
    llm_result = {"mapping": {str(i): new_path for i in range(len(paths_list))}}

    with patch("manager.categorize.sheets_cache.read_cache", return_value={}), \
         patch("manager.categorize.sheets_cache.write_cache"), \
         patch("manager.categorize.llm_structured", return_value=llm_result):
        _normalize_categories(cache, names)

    assert cache["M00"]["old_path"] == ["Root00", "Sub"]
    assert cache["M00"]["path"] == new_path


# ── Cache hit: no LLM ─────────────────────────────────────────────────────────

def test_normalize_skips_llm_on_cache_hit():
    """When the path-set hash matches a cached mapping, LLM must not be called."""
    import hashlib
    roots = _many_roots(16)
    cache, names = _make_cache(roots)

    # Compute the hash the same way the implementation does
    paths_list = sorted({tuple(v["path"]) for v in cache.values()})
    paths_hash = hashlib.sha256(
        json.dumps([list(p) for p in paths_list]).encode()
    ).hexdigest()[:16]

    # Pre-populate consolidation cache with a mapping for this hash
    cached_mapping = {json.dumps(list(p)): ["Cached"] for p in paths_list}
    consolidation_cache = {paths_hash: cached_mapping}

    with patch("manager.categorize.sheets_cache.read_cache", return_value=consolidation_cache), \
         patch("manager.categorize.sheets_cache.write_cache"), \
         patch("manager.categorize.llm_structured") as mock_llm:
        _normalize_categories(cache, names)

    mock_llm.assert_not_called()


def test_normalize_applies_cached_mapping_correctly():
    """Paths from the consolidation cache must be applied to merchant entries."""
    import hashlib
    roots = _many_roots(16)
    cache, names = _make_cache(roots)

    paths_list = sorted({tuple(v["path"]) for v in cache.values()})
    paths_hash = hashlib.sha256(
        json.dumps([list(p) for p in paths_list]).encode()
    ).hexdigest()[:16]

    new_path = ["Cached", "Path"]
    cached_mapping = {json.dumps(list(p)): new_path for p in paths_list}
    consolidation_cache = {paths_hash: cached_mapping}

    with patch("manager.categorize.sheets_cache.read_cache", return_value=consolidation_cache), \
         patch("manager.categorize.sheets_cache.write_cache"), \
         patch("manager.categorize.llm_structured"):
        _normalize_categories(cache, names)

    for name in names:
        assert cache[name]["path"] == new_path


def test_normalize_does_not_write_consolidation_cache_on_hit():
    """A cache hit must not trigger a write to the consolidation cache tab."""
    import hashlib
    roots = _many_roots(16)
    cache, names = _make_cache(roots)

    paths_list = sorted({tuple(v["path"]) for v in cache.values()})
    paths_hash = hashlib.sha256(
        json.dumps([list(p) for p in paths_list]).encode()
    ).hexdigest()[:16]

    cached_mapping = {json.dumps(list(p)): ["Cached"] for p in paths_list}
    consolidation_cache = {paths_hash: cached_mapping}
    write_calls: list = []

    def fake_write(tab, data):
        write_calls.append(tab)

    with patch("manager.categorize.sheets_cache.read_cache", return_value=consolidation_cache), \
         patch("manager.categorize.sheets_cache.write_cache", side_effect=fake_write), \
         patch("manager.categorize.llm_structured"):
        _normalize_categories(cache, names)

    # The consolidation cache tab must NOT be written on a hit
    assert "_consolidation_cache" not in write_calls
