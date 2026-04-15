import json
from collections import defaultdict
from datetime import datetime, timedelta

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


def _load_cache() -> dict:
    return sheets_cache.read_cache(_CACHE_TAB)


def _save_cache(cache: dict) -> None:
    sheets_cache.write_cache(_CACHE_TAB, cache)


_CATEGORIZE_RULES = """Rules for the path array:
- Use 2 levels for clear-cut merchants (e.g. ["Utilities", "Internet"]).
- Use 3 levels where meaningful specificity exists (e.g. ["Food & Drink", "Restaurants", "Fast Food"]).
- Use 4 levels only when genuinely distinct (e.g. ["Shopping", "Clothing", "Kids", "Shoes"]).
- Do not invent a deeper level just to fill space."""


def categorize_with_llm(groups: list[dict]) -> list[dict]:
    cache = _load_cache()
    to_be_cached = {g["name"] for g in groups if g["name"] not in cache}

    if to_be_cached:
        print(f"Sending {len(to_be_cached)} merchant(s) to LLM for categorization... ({len(cache)} cached)")
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

        # Send up to _BATCH_SIZE merchants per LLM call
        for start in range(0, len(merchants), _BATCH_SIZE):
            chunk = merchants[start : start + _BATCH_SIZE]
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
        for g in merchants:
            if g["name"] not in cache:
                cache[g["name"]] = {"path": ["Uncategorized"]}

        _save_cache(cache)
        print(f"Classified {len(to_be_cached)} new merchants.")

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
            # Keep merging while the current node has exactly one child
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

    print(
        f"Found {len(current_paths)} unique paths across {len(root_cats)} root categories "
        f"— consolidating to max 100 paths / 15 roots..."
    )

    paths_list = sorted(current_paths)
    paths_display = "\n".join(f"{i}: {json.dumps(list(p))}" for i, p in enumerate(paths_list))

    prompt = f"""You are a personal finance taxonomy expert. The following category paths are used to classify merchants:

{paths_display}

Consolidate them into at most 100 unique paths with at most 15 root categories.
Merge only truly similar categories; preserve meaningful specificity where it matters.
All {len(paths_list)} indices must be present in the mapping."""

    result = llm_structured(prompt, _NORMALIZE_SCHEMA, "normalize_categories")
    mapping = {paths_list[int(i)]: new_path for i, new_path in result["mapping"].items()}

    changed = 0
    for name in merchant_names:
        entry = cache.get(name)
        if not entry or "path" not in entry:
            continue
        old_tuple = tuple(entry["path"])
        new_path = mapping.get(old_tuple)
        if new_path is not None and new_path != entry["path"]:
            if "old_path" not in entry:  # preserve original for debugging, never overwrite
                entry["old_path"] = entry["path"]
            entry["path"] = new_path
            changed += 1

    if changed:
        _save_cache(cache)
    print(f"Consolidated {changed} merchant(s) to the new taxonomy.")

# ── Transaction pipeline ───────────────────────────────────────────────────────

def filter_by_date(transactions: list[dict], days: int = 365) -> list[dict]:
    """Keep only transactions within the last `days` days from today."""
    cutoff = datetime.today() - timedelta(days=days)
    return [
        t for t in transactions
        if datetime.strptime(t["date"], "%Y-%m-%d") >= cutoff
    ]


def group_by_month_merchant(transactions: list[dict]) -> list[dict]:
    """Aggregate transactions by (month, merchant), summing amounts."""
    groups: dict[tuple, dict] = defaultdict(lambda: {"amount": 0.0, "count": 0, "category": "Uncategorized"})

    for t in transactions:
        month = t["date"][:7]  # "YYYY-MM"
        key = (month, t["name"])
        groups[key]["amount"] += t["amount"]
        groups[key]["count"] += 1
        groups[key]["category"] = t["category"]

    return [
        {"month": month, "name": name, **data}
        for (month, name), data in sorted(groups.items())
    ]


def filter_transactions(transactions: list[dict], days_window: int = 7, amount_tolerance: float = 0.01) -> list[dict]:
    """Remove dust (< $0.10) and paired transfer transactions (opposite-sign, matching amount within days_window days)."""
    transactions = [t for t in transactions if abs(t["amount"]) >= 0.1]
    removed: set[int] = set()

    for i in range(len(transactions)):
        if i in removed:
            continue
        t1 = transactions[i]
        try:
            d1 = datetime.strptime(t1["date"], "%Y-%m-%d")
        except ValueError:
            continue

        for j in range(i + 1, len(transactions)):
            if j in removed:
                continue
            t2 = transactions[j]

            if not ((t1["amount"] > 0 and t2["amount"] < 0) or (t1["amount"] < 0 and t2["amount"] > 0)):
                continue
            if abs(abs(t1["amount"]) - abs(t2["amount"])) > amount_tolerance:
                continue

            try:
                d2 = datetime.strptime(t2["date"], "%Y-%m-%d")
            except ValueError:
                continue

            if abs((d1 - d2).days) <= days_window:
                removed.add(i)
                removed.add(j)
                break

    if removed:
        pairs = len(removed) // 2
        for i in sorted(removed):
            t = transactions[i]

    return [t for i, t in enumerate(transactions) if i not in removed]

