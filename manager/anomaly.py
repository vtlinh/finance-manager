import json
import os
from collections import defaultdict
from datetime import date
from pathlib import Path

from .llm import llm_structured

_ANOMALY_SCHEMA = {
    "type": "object",
    "properties": {
        "anomalies": {
            "type": "array",
            "description": "Notable spending anomalies; empty array if nothing unusual",
            "items": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "Concise description of the anomaly, including dollar amounts",
                    },
                    "amount": {
                        "type": "number",
                        "description": "Dollar amount most relevant to the anomaly (positive number)",
                    },
                },
                "required": ["note", "amount"],
            },
        }
    },
    "required": ["anomalies"],
}

ANOMALY_CACHE_FILE = Path(__file__).parent / ".anomaly_cache.json"
ANOMALY_DURATION_WINDOW = 12


def load_anomaly_cache() -> dict:
    if os.path.exists(ANOMALY_CACHE_FILE):
        with open(ANOMALY_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_anomaly_cache(cache: dict) -> None:
    with open(ANOMALY_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def _monthly_totals_by_top_cat(groups: list[dict]) -> dict[str, dict[str, float]]:
    """Returns {month: {top_category: total}}."""
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for g in groups:
        top = g["path"][0] if g.get("path") else "Uncategorized"
        totals[g["month"]][top] += g["amount"]
    return totals


def detect_monthly_anomalies(groups: list[dict]) -> dict:
    """For each uncached past month with 6+ prior months of data, ask LLM for anomaly notes."""
    anomaly_cache = load_anomaly_cache()

    monthly_totals = _monthly_totals_by_top_cat(groups)
    current_month = date.today().strftime("%Y-%m")
    months = sorted(m for m in monthly_totals if m != current_month)

    to_analyze = [
        m for i, m in enumerate(months)
        if m not in anomaly_cache and i >= ANOMALY_DURATION_WINDOW
    ]

    if not to_analyze:
        cached_count = sum(1 for m in months if m in anomaly_cache)
        print(f"All {cached_count} month(s) loaded from anomaly cache — no LLM call needed.\n")
        return anomaly_cache

    print(f"Analyzing {len(to_analyze)} month(s) for spending anomalies...")
    for month in to_analyze:
        print(f"  Analyzing {month}...")
        idx = months.index(month)
        prior = months[max(0, idx - ANOMALY_DURATION_WINDOW):idx]

        all_cats = sorted({
            cat
            for m in [*prior, month]
            for cat in monthly_totals[m]
        })

        # Build comparison table for the prompt
        col_w = 10
        header = "Category".ljust(28) + "".join(m.rjust(col_w) for m in [*prior, f"{month}*"])
        divider = "-" * len(header)
        rows = [header, divider]
        for cat in all_cats:
            row = cat[:28].ljust(28)
            for m in [*prior, month]:
                amt = monthly_totals[m].get(cat, 0.0)
                row += (f"{amt:,.0f}" if amt else "").rjust(col_w)
            rows.append(row)

        prompt = f"""You are a personal finance analyst reviewing monthly spending.
* marks the month being analyzed.

{chr(10).join(rows)}

Identify notable anomalies in {month} vs the reference months.
Focus on: large increases (>30%), large decreases (>30%), categories that appeared or disappeared.
Be concise and specific (include dollar amounts).
Return an empty anomalies list if nothing is unusual."""

        result = llm_structured(prompt, _ANOMALY_SCHEMA, "report_anomalies")
        items = result["anomalies"]
        items.sort(key=lambda x: x.get("amount", 0), reverse=True)
        anomaly_cache[month] = [item["note"] for item in items]

    save_anomaly_cache(anomaly_cache)
    print(f"Anomaly analysis complete.\n")
    return anomaly_cache
