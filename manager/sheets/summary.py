from collections import defaultdict

import gspread

from ..llm import llm_structured
from .helpers import _month_label

_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "insights": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Interesting spending observations sorted by dollar impact (largest first)",
        }
    },
    "required": ["insights"],
}


def _generate_summary_insights(
    year_groups: list[dict],
    prev_year_groups: list[dict],
    year: str,
) -> list[str]:
    """Ask the LLM for notable year-over-year spending insights."""

    def top_totals(groups: list[dict]) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for g in groups:
            top = g.get("path", ["Uncategorized"])[0]
            totals[top] += g["amount"]
        return totals

    curr = {cat: round(-amt, 2) for cat, amt in top_totals(year_groups).items()}
    prev = {cat: round(-amt, 2) for cat, amt in top_totals(prev_year_groups).items()} if prev_year_groups else {}
    prev_year = str(int(year) - 1)

    curr_total = round(sum(curr.values()), 2)
    prev_total = round(sum(prev.values()), 2) if prev else None

    all_cats = sorted(set(curr) | set(prev), key=lambda c: -curr.get(c, 0))
    lines = []
    if prev:
        lines.append(f"{'Category':<30} {prev_year:>10} {year:>10} {'Change':>10} {'%':>7}")
        lines.append("-" * 69)
        for cat in all_cats:
            c = curr.get(cat, 0)
            p = prev.get(cat, 0)
            chg = c - p
            pct = f"{chg/p*100:+.1f}%" if p else "new"
            lines.append(f"{cat:<30} {p:>10,.0f} {c:>10,.0f} {chg:>+10,.0f} {pct:>7}")
        lines.append("-" * 69)
        lines.append(f"{'Total':<30} {prev_total:>10,.0f} {curr_total:>10,.0f} {curr_total-prev_total:>+10,.0f} {(curr_total-prev_total)/prev_total*100:>+6.1f}%")
    else:
        lines.append(f"{'Category':<30} {year:>10}")
        lines.append("-" * 42)
        for cat in all_cats:
            lines.append(f"{cat:<30} {curr.get(cat,0):>10,.0f}")
        lines.append("-" * 42)
        lines.append(f"{'Total':<30} {curr_total:>10,.0f}")

    table = "\n".join(lines)
    has_prev = bool(prev)

    prompt = f"""You are a personal finance analyst reviewing annual spending.

{'Year-over-year comparison:' if has_prev else f'Spending summary for {year}:'}

{table}

{'Identify the most interesting changes from ' + prev_year + ' to ' + year + '.' if has_prev else f'Summarize the key spending patterns for {year}.'}
Focus on facts that involve the largest dollar amounts.
Be concise and specific — include dollar figures and percentages.
Skip obvious or trivial observations.
Return 4–7 insights sorted by financial impact (largest first)."""

    result = llm_structured(prompt, _SUMMARY_SCHEMA, "report_summary")
    return result.get("insights", [])


def write_summary_sheet(
    ws: gspread.Worksheet,
    year_groups: list[dict],
    prev_year_groups: list[dict],
    anomaly_cache: dict,
    year: str,
    insights: list[str],
) -> None:
    """Write a summary tab: LLM insights then anomalies."""

    rows: list[list] = [
        [f"{year} Summary"],
        [""],
    ]
    for insight in insights:
        rows.append([f"• {insight}"])

    year_months = sorted({g["month"] for g in year_groups})
    noted = [(m, anomaly_cache.get(m, [])) for m in year_months if anomaly_cache.get(m)]

    rows.append([""])
    anomaly_title_row = len(rows) + 1
    rows.append(["Spending Anomalies"])
    rows.append(["Month", "Anomaly"])
    if noted:
        for m, notes in noted:
            for i, note in enumerate(notes):
                rows.append([_month_label(m, include_year=False) if i == 0 else "", note])
    else:
        rows.append(["", "No anomalies detected."])

    ws.clear()
    ws.update(rows, value_input_option="USER_ENTERED")

    ws.format("A1", {"textFormat": {"bold": True, "fontSize": 14}})
    ws.format(f"A{anomaly_title_row}", {"textFormat": {"bold": True}})
    ws.format(f"A{anomaly_title_row + 1}:B{anomaly_title_row + 1}", {"textFormat": {"bold": True}})

    ws.spreadsheet.batch_update({"requests": [
        {"autoResizeDimensions": {"dimensions": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 2}}},
    ]})
