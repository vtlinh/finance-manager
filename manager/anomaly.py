from collections import defaultdict
from datetime import date

from .llm import llm_batch_structured
from . import sheets_cache

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

_CACHE_TAB = "_anomaly_cache"
ANOMALY_DURATION_WINDOW = 12


def _monthly_totals_by_top_cat(groups: list[dict]) -> dict[str, dict[str, float]]:
    """Returns {month: {top_category: total}}."""
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for g in groups:
        top = g["path"][0] if g.get("path") else "Uncategorized"
        totals[g["month"]][top] += g["amount"]
    return totals


def _dominant_merchants(groups: list[dict], monthly_totals: dict, threshold: float = 0.20) -> dict:
    """Returns {month: [(merchant, category, spend, pct), ...]} for merchant groups whose
    absolute spend is >= threshold of their top-level category total for that month."""
    result: dict[str, list] = defaultdict(list)
    for g in groups:
        top = g["path"][0] if g.get("path") else "Uncategorized"
        month = g["month"]
        cat_total = abs(monthly_totals[month].get(top, 0.0))
        if cat_total == 0:
            continue
        spend = abs(g["amount"])
        pct = spend / cat_total
        if pct >= threshold:
            result[month].append((g["name"], top, spend, pct))
    for month in result:
        result[month].sort(key=lambda x: x[2], reverse=True)
    return dict(result)


def detect_monthly_anomalies(groups: list[dict]) -> dict:
    """For each uncached past month with 6+ prior months of data, ask LLM for anomaly notes."""
    anomaly_cache = sheets_cache.read_cache(_CACHE_TAB)

    monthly_totals = _monthly_totals_by_top_cat(groups)
    current_month = date.today().strftime("%Y-%m")
    months = sorted(m for m in monthly_totals if m != current_month)

    to_analyze = [
        m for i, m in enumerate(months)
        if m not in anomaly_cache and i >= ANOMALY_DURATION_WINDOW
    ]

    if not to_analyze:
        cached_count = sum(1 for m in months if m in anomaly_cache)
        print(f"All {cached_count} month(s) loaded from anomaly cache — no LLM call needed.")
        return anomaly_cache

    print(f"Analyzing {len(to_analyze)} month(s) for spending anomalies...", flush=True)

    dominant = _dominant_merchants(groups, monthly_totals)

    prompts: list[tuple[str, str]] = []
    for month in to_analyze:
        idx = months.index(month)
        prior = months[max(0, idx - ANOMALY_DURATION_WINDOW):idx]

        all_cats = sorted({
            cat
            for m in [*prior, month]
            for cat in monthly_totals[m]
        })

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

        big = dominant.get(month, [])
        merchant_hint = ""
        if big:
            lines = ["", "Single transactions ≥20% of their category in this month:"]
            for name, cat, spend, pct in big:
                lines.append(f"  • {name}: ${spend:,.0f} ({pct:.0%} of {cat})")
            merchant_hint = "\n".join(lines)

        prompt = f"""You are a personal finance analyst reviewing monthly spending.
* marks the month being analyzed.

{chr(10).join(rows)}{merchant_hint}

Identify notable anomalies in {month} vs the reference months.
Focus on: large increases (>30%), large decreases (>30%), categories that appeared or disappeared.
When a dominant transaction is listed above, mention the merchant by name in your note.
Be concise and specific (include dollar amounts).
Return an empty anomalies list if nothing is unusual."""

        prompts.append((month, prompt))

    results = llm_batch_structured(prompts, _ANOMALY_SCHEMA, "report_anomalies")

    for month in to_analyze:
        items = results.get(month, {}).get("anomalies", [])
        items.sort(key=lambda x: x.get("amount", 0), reverse=True)
        anomaly_cache[month] = [item["note"] for item in items]

    sheets_cache.write_cache(_CACHE_TAB, anomaly_cache)
    print("Anomaly analysis complete.")
    return anomaly_cache
