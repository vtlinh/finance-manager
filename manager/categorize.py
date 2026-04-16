import hashlib
import json
from collections import defaultdict

from .llm import llm_structured
from . import sheets_cache

# ── JSON schemas for structured LLM output ────────────────────────────────────

_CATEGORIZE_BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "categories": {
            "type": "object",
            "description": "Maps each merchant index (as a string) to its category path array",
            "additionalProperties": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 4,
            },
        }
    },
    "required": ["categories"],
}

_BATCH_SIZE = 1000

_NORMALIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "mapping": {
            "type": "object",
            "description": "Maps each input index (as a string) to its consolidated category path",
            "additionalProperties": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
        }
    },
    "required": ["mapping"],
}

_CACHE_TAB = "_merchant_cache"
_CONSOLIDATION_CACHE_TAB = "_consolidation_cache"

# Monarch category values that carry no real information and should be ignored
_JUNK_CATEGORIES: frozenset[str] = frozenset({
    "", "unknown", "uncategorized", "other", "others",
    "no category", "none", "misc", "miscellaneous",
})


def _is_junk_category(category: str) -> bool:
    return category.strip().lower() in _JUNK_CATEGORIES


_CATEGORIZE_RULES = """Rules for the path array:
- Use 2 levels for clear-cut merchants (e.g. ["Utilities", "Internet"]).
- Use 3 levels where meaningful specificity exists (e.g. ["Food & Drink", "Restaurants", "Fast Food"]).
- Use 4 levels only when genuinely distinct (e.g. ["Shopping", "Clothing", "Kids", "Shoes"]).
- Do not invent a deeper level just to fill space."""


def _load_cache() -> dict:
    return sheets_cache.read_cache(_CACHE_TAB)


def _save_cache(cache: dict) -> None:
    sheets_cache.write_cache(_CACHE_TAB, cache)


def categorize_with_llm(groups: list[dict]) -> list[dict]:
    cache = _load_cache()
    to_be_cached = {g["name"] for g in groups if g["name"] not in cache}

    if to_be_cached:
        print(f"Processing {len(to_be_cached)} new merchant(s)... ({len(cache)} cached)")
    else:
        print(f"{len(cache)} merchants cached.")

    if to_be_cached:
        # Build one representative entry per uncached merchant (preserving first occurrence)
        seen: set[str] = set()
        merchants: list[dict] = []
        for g in groups:
            if g["name"] in to_be_cached and g["name"] not in seen:
                merchants.append(g)
                seen.add(g["name"])

        # Adopt non-junk Monarch categories directly; only send the rest to LLM
        adopted = 0
        for g in merchants:
            if not _is_junk_category(g["category"]):
                cache[g["name"]] = {"path": [g["category"]]}
                adopted += 1
        if adopted:
            print(f"Adopted Monarch category for {adopted} merchant(s).")

        merchants_for_llm = [g for g in merchants if g["name"] not in cache]
        if merchants_for_llm:
            print(f"Sending {len(merchants_for_llm)} merchant(s) to LLM for categorization...")
            # Send up to _BATCH_SIZE merchants per LLM call
            for start in range(0, len(merchants_for_llm), _BATCH_SIZE):
                chunk = merchants_for_llm[start : start + _BATCH_SIZE]
                lines = "\n".join(
                    f"{i}: {g['name']} | ${g['amount']:.2f} ({g['count']}x) | existing: {g['category']}"
                    for i, g in enumerate(chunk)
                )
                prompt = (
                    "You are a personal finance assistant. "
                    "Categorize each merchant below into a hierarchical category path.\n\n"
                    + lines + "\n\n"
                    + _CATEGORIZE_RULES
                    + '\n\nReturn a "categories" object mapping each index (as a string) to its path array.'
                )
                result = llm_structured(prompt, _CATEGORIZE_BATCH_SCHEMA, "categorize_merchants")
                for idx_str, path in result.get("categories", {}).items():
                    if idx_str.isdigit() and int(idx_str) < len(chunk):
                        cache[chunk[int(idx_str)]["name"]] = {"path": path}

            # Fill any index the model skipped
            for g in merchants_for_llm:
                if g["name"] not in cache:
                    cache[g["name"]] = {"path": ["Uncategorized"]}

        _save_cache(cache)
        print(f"Classified {len(to_be_cached)} new merchants ({adopted} from Monarch, {len(merchants_for_llm)} via LLM).")

    merchant_names = {g["name"] for g in groups}
    _collapse_single_child_categories(cache, merchant_names)
    _normalize_categories(cache, merchant_names)

    categorized = []
    for g in groups:
        path = cache.get(g["name"], {}).get("path", ["Uncategorized"])
        categorized.append({**g, "path": path})

    return categorized


def _collapse_single_child_categories(cache: dict, merchant_names: set[str]) -> None:
    """Merge any path level that has only one child into 'Parent > Child'."""
    current_paths = [
        tuple(cache[name]["path"])
        for name in merchant_names
        if name in cache and "path" in cache[name]
    ]

    # Build children map: path prefix -> set of next elements
    children: dict[tuple, set] = defaultdict(set)
    for path in current_paths:
        for i in range(len(path)):
            children[path[:i]].add(path[i])

    def compress(path: tuple) -> list[str]:
        new_path = []
        i = 0
        while i < len(path):
            segment = path[i]
            while len(children[path[:i + 1]]) == 1 and i + 1 < len(path):
                i += 1
                segment += " > " + path[i]
            new_path.append(segment)
            i += 1
        return new_path

    changed = 0
    for name in merchant_names:
        entry = cache.get(name)
        if not entry or "path" not in entry:
            continue
        new_path = compress(tuple(entry["path"]))
        if new_path != entry["path"]:
            if "old_path" not in entry:
                entry["old_path"] = entry["path"]
            entry["path"] = new_path
            changed += 1

    if changed:
        _save_cache(cache)
        print(f"Collapsed {changed} merchant(s) with single-child category levels.")


def _normalize_categories(cache: dict, merchant_names: set[str]) -> None:
    """If merchants in use span >100 unique paths or >15 root categories, consolidate via LLM."""
    current_paths = {
        tuple(cache[name]["path"])
        for name in merchant_names
        if name in cache and "path" in cache[name]
    }

    root_cats = {p[0] for p in current_paths}
    if len(current_paths) <= 100 and len(root_cats) <= 15:
        return

    # Hash the path-set so we can skip the LLM if we've already consolidated this exact taxonomy
    paths_list = sorted(current_paths)
    paths_hash = hashlib.sha256(
        json.dumps([list(p) for p in paths_list]).encode()
    ).hexdigest()[:16]

    consolidation_cache = sheets_cache.read_cache(_CONSOLIDATION_CACHE_TAB)
    cached_mapping_raw = consolidation_cache.get(paths_hash)
    if cached_mapping_raw is not None:
        # Cached mapping is stored as {json_str_of_path: new_path_list}
        mapping = {tuple(json.loads(k)): v for k, v in cached_mapping_raw.items()}
        print(f"Applying cached consolidation mapping ({len(mapping)} paths).")
    else:
        print(
            f"Found {len(current_paths)} unique paths across {len(root_cats)} root categories "
            f"— consolidating to max 100 paths / 15 roots..."
        )

        paths_display = "\n".join(f"{i}: {json.dumps(list(p))}" for i, p in enumerate(paths_list))

        prompt = f"""You are a personal finance taxonomy expert. The following category paths are used to classify merchants:

{paths_display}

Consolidate them into at most 100 unique paths with at most 15 root categories.
Merge only truly similar categories; preserve meaningful specificity where it matters.
All {len(paths_list)} indices must be present in the mapping."""

        result = llm_structured(prompt, _NORMALIZE_SCHEMA, "normalize_categories")
        mapping = {paths_list[int(i)]: new_path for i, new_path in result["mapping"].items()}

        # Persist the mapping keyed by the path-set hash
        serializable = {json.dumps(list(k)): v for k, v in mapping.items()}
        consolidation_cache[paths_hash] = serializable
        sheets_cache.write_cache(_CONSOLIDATION_CACHE_TAB, consolidation_cache)

    changed = 0
    for name in merchant_names:
        entry = cache.get(name)
        if not entry or "path" not in entry:
            continue
        old_tuple = tuple(entry["path"])
        new_path = mapping.get(old_tuple)
        if new_path is not None and new_path != entry["path"]:
            if "old_path" not in entry:
                entry["old_path"] = entry["path"]
            entry["path"] = new_path
            changed += 1

    if changed:
        _save_cache(cache)
    print(f"Consolidated {changed} merchant(s) to the new taxonomy.")
