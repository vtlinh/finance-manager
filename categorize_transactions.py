import asyncio
import calendar
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

from anomaly import detect_monthly_anomalies
from export_to_sheets import export
from llm import categorize_with_llm
from load_transactions import get_transactions

load_dotenv()

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


def filter_transfers(transactions: list[dict], days_window: int = 3, amount_tolerance: float = 0.01) -> list[dict]:
    """Remove paired transfer transactions: matching absolute amounts with opposite signs within days_window days."""
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


# ── Spreadsheet output ─────────────────────────────────────────────────────────

def _month_label(m: str) -> str:
    """'2025-01' -> "Jan '25" (or "Jan '25 (partial)" if it's the current incomplete month)."""
    label = datetime.strptime(m, "%Y-%m").strftime("%b '%y")
    today = date.today()
    if m == today.strftime("%Y-%m"):
        last_day = calendar.monthrange(today.year, today.month)[1]
        if today.day != last_day:
            label += " (partial)"
    return label


def print_spreadsheet(groups: list[dict], anomaly_cache: dict) -> None:
    months = sorted({g["month"] for g in groups})

    # Build tree: each node holds per-month amounts and child nodes
    def new_node() -> dict:
        return {"amounts": defaultdict(float), "children": {}}

    root = new_node()
    for g in groups:
        path = g.get("path", ["Uncategorized"])
        node = root
        node["amounts"][g["month"]] += g["amount"]
        for part in path:
            node["children"].setdefault(part, new_node())
            node = node["children"][part]
            node["amounts"][g["month"]] += g["amount"]

    # Layout
    CAT_W  = 34   # category label column width
    COL_W  = 9    # minimum per-month column width
    TOT_W  = 12   # year-total column width
    INDENT = 2    # spaces per depth level

    # Column list: month entries, with a year-total entry inserted after each December
    cols: list[tuple[str, str]] = []
    for m in months:
        cols.append(("month", m))
        if m.endswith("-12"):
            cols.append(("year", m[:4]))

    month_set = set(months)

    def col_label(col: tuple[str, str]) -> str:
        kind, val = col
        if kind == "month":
            return _month_label(val)
        all_12 = all(f"{val}-{m:02d}" in month_set for m in range(1, 13))
        return val if all_12 else f"{val} (partial)"

    col_labels = [col_label(c) for c in cols]
    col_widths  = [TOT_W if c[0] == "year" else max(COL_W, len(lbl))
                   for c, lbl in zip(cols, col_labels)]

    def fmt_amt(v: float) -> str:
        return f"{v:,.0f}" if v else ""

    def col_amt(node: dict, col: tuple[str, str], negate: bool) -> float:
        kind, val = col
        if kind == "month":
            amt = node["amounts"].get(val, 0.0)
        else:  # year total: sum all months of that year
            amt = sum(node["amounts"].get(m, 0.0) for m in months if m.startswith(val + "-"))
        return -amt if negate else amt

    # ── Header ──
    header = "Category".ljust(CAT_W)
    for lbl, w in zip(col_labels, col_widths):
        header += lbl.rjust(w)
    sep = "─" * len(header)

    print(sep)
    print(header)
    print(sep)

    # ── Rows (recursive) ──
    def print_rows(node: dict, depth: int) -> None:
        indent = " " * (depth * INDENT)
        for name, child in sorted(node["children"].items()):
            if not any(child["amounts"].get(m, 0.0) for m in months):
                continue
            label = (indent + name)[:CAT_W]
            row = label.ljust(CAT_W)
            for col, w in zip(cols, col_widths):
                row += fmt_amt(col_amt(child, col, negate=True)).rjust(w)
            print(row)
            print_rows(child, depth + 1)

    print_rows(root, 0)

    # ── Total row ──
    print(sep)
    total_row = "Total".ljust(CAT_W)
    for col, w in zip(cols, col_widths):
        total_row += fmt_amt(col_amt(root, col, negate=False)).rjust(w)
    print(total_row)
    print(sep)

    # ── Anomaly notes ──
    noted = [(m, anomaly_cache[m]) for m in months if anomaly_cache.get(m)]
    if noted:
        print()
        print("Spending Anomalies")
        print("─" * 50)
        for m, notes in noted:
            print(f"\n  {_month_label(m)} ({m}):")
            for note in notes:
                print(f"    • {note}")


# ── Entry point ────────────────────────────────────────────────────────────────

async def main() -> None:
    transactions = await get_transactions()
    print(f"Loaded {len(transactions)} transactions.\n")

    transactions = filter_transfers(transactions)
    groups = group_by_month_merchant(transactions)

    categorized = categorize_with_llm(groups)
    anomalies = detect_monthly_anomalies(categorized)

    print()
    print_spreadsheet(categorized, anomalies)
    export(categorized, anomalies)


if __name__ == "__main__":
    asyncio.run(main())
