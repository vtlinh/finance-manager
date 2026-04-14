import json
import os

import anthropic

CACHE_FILE = ".merchant_cache.json"


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        # Migrate old format (category + subcategory) to path list
        for entry in cache.values():
            if "path" not in entry and "category" in entry:
                entry["path"] = [entry["category"]]
                if entry.get("subcategory") and entry["subcategory"] != "Other":
                    entry["path"].append(entry["subcategory"])
        return cache
    return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def strip_code_fences(text: str) -> str:
    """Remove markdown code fences (```json ... ``` or ``` ... ```) if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text[text.index("\n") + 1:]  # drop the opening ``` line
    if text.endswith("```"):
        text = text[:text.rindex("```")]
    return text.strip()


def llm_prompt(prompt: str) -> str:
    client = anthropic.Anthropic()
    with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=128000,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
        message = stream.get_final_message()
        text = ''.join(b.text for b in message.content if b.type == "text")
        if not text:
            raise ValueError(f"No text block in LLM response (got block types: {[b.type for b in message.content]})")
        return strip_code_fences(text)


def build_prompt(groups: list[dict]) -> str:
    lines = []
    for i, g in enumerate(groups):
        lines.append(f"{i}: {g['month']} | {g['name']} | ${g['amount']:.2f} ({g['count']}x) | {g['category']}")
    return "\n".join(lines)


def categorize_with_llm(groups: list[dict]) -> list[dict]:
    cache = load_cache()
    to_be_cached = {g["name"] for g in groups if g["name"] not in cache}

    if to_be_cached:
        print(f"Sending {len(to_be_cached)} merchant(s) to LLM for categorization..., {len(cache)} merchants cached.")
    else:
        print(f"{len(cache)} merchants cached.")

    while to_be_cached:
        # limit to 1000 to avoid overwhelming the LLM (and hitting token limits)
        uncached_names = sorted(to_be_cached)[:1000]
        representative: dict[str, dict] = {}
        for g in groups:
            if g["name"] in uncached_names and g["name"] not in representative:
                representative[g["name"]] = g

        to_classify = [{"index": i, **representative[name]} for i, name in enumerate(uncached_names)]
        prompt = f"""You are a personal finance assistant. Categorize each merchant into a hierarchical category path.

Merchants (index | month | merchant | total amount (count) | existing category):
{build_prompt(to_classify)}

Return a JSON array with one object per merchant, in the same order, with these fields:
- index: the merchant index (integer)
- path: array of strings from broadest to most specific category
  Use 2 levels for clear-cut merchants (e.g. ["Utilities", "Internet"]).
  Use 3 levels where meaningful specificity exists (e.g. ["Food & Drink", "Restaurants", "Fast Food"]).
  Use 4 levels only when genuinely distinct (e.g. ["Shopping", "Clothing", "Kids", "Shoes"]).
  Do not invent a deeper level just to fill space.

Return ONLY the JSON array, no other text."""
        for r in json.loads(llm_prompt(prompt)):
            name = uncached_names[r["index"]]
            cache[name] = {"path": r["path"]}

        save_cache(cache)
        print(f"Classified {len(uncached_names)} new merchants.")
        to_be_cached -= set(uncached_names)

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
    from collections import defaultdict

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
        save_cache(cache)
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

Return a JSON object where each key is an index (as a string) and each value is the new path (array of strings).
All {len(paths_list)} indices must be present. Return ONLY the JSON object, no other text."""

    result = json.loads(llm_prompt(prompt))
    mapping = {paths_list[int(i)]: new_path for i, new_path in result.items()}

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

    save_cache(cache)
    print(f"Consolidated {changed} merchant(s) to the new taxonomy.")
