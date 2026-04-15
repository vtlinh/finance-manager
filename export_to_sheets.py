"""Export the finance summary to a Google Sheet."""
import calendar
import os
import sys
from collections import defaultdict
from datetime import date, datetime

sys.stdout.reconfigure(encoding="utf-8")

import gspread
from dotenv import load_dotenv

from login import Google

load_dotenv()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _month_label(m: str, include_year: bool = False) -> str:
    fmt = "%b '%y" if include_year else "%b"
    label = datetime.strptime(m, "%Y-%m").strftime(fmt)
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
            return _month_label(val, include_year=True)
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
            print(f"\n  {_month_label(m, include_year=True)} ({m}):")
            for note in notes:
                print(f"    • {note}")


# ── Sheet builders ─────────────────────────────────────────────────────────────

def _build_tree(groups: list[dict]) -> tuple[dict, list[str]]:
    months = sorted({g["month"] for g in groups})

    def new_node():
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

    return root, months


def write_spending_sheet(ws: gspread.Worksheet, groups: list[dict], anomaly_cache: dict) -> None:
    root, months = _build_tree(groups)

    month_set = set(months)
    year = months[0][:4]
    all_12 = all(f"{year}-{m:02d}" in month_set for m in range(1, 13))
    total_label = "Total" if all_12 else "Total (partial)"

    col_labels = [_month_label(m) for m in months]

    rows = [["Category"] + col_labels + [total_label]]

    def add_rows(node: dict, depth: int) -> None:
        indent = "  " * depth
        for name, child in sorted(node["children"].items()):
            if not any(child["amounts"].get(m, 0.0) for m in months):
                continue
            row = [indent + name]
            year_total = 0.0
            for m in months:
                amt = -child["amounts"].get(m, 0.0)
                year_total += amt
                row.append(round(amt, 2) if amt else "")
            row.append(round(year_total, 2))
            rows.append(row)
            add_rows(child, depth + 1)

    add_rows(root, 0)

    total_row = ["Total"]
    grand = 0.0
    for m in months:
        amt = root["amounts"].get(m, 0.0)
        grand += amt
        total_row.append(round(amt, 2) if amt else "")
    total_row.append(round(grand, 2))
    rows.append(total_row)

    ws.clear()
    ws.update(rows, value_input_option="USER_ENTERED")

    n_cols = len(rows[0])
    last_row = len(rows)
    a1_header = gspread.utils.rowcol_to_a1(1, n_cols)
    a1_total = gspread.utils.rowcol_to_a1(last_row, n_cols)
    ws.format(f"A1:{a1_header}", {"textFormat": {"bold": True}})
    ws.format(f"A{last_row}:{a1_total}", {"textFormat": {"bold": True}})

    # Currency formatting for all number cells (skip header row and category column)
    a1_nums = f"B2:{gspread.utils.rowcol_to_a1(last_row, n_cols)}"
    ws.format(a1_nums, {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}})

    ws.freeze(rows=1, cols=1)

    ws.spreadsheet.batch_update({"requests": [
        {"autoResizeDimensions": {"dimensions": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": n_cols}}},
        {"autoResizeDimensions": {"dimensions": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": 0, "endIndex": last_row}}},
    ]})


def write_anomalies_sheet(ws: gspread.Worksheet, anomaly_cache: dict, months: list[str]) -> None:
    rows = [["Month", "Anomaly"]]
    for m in months:
        for i, note in enumerate(anomaly_cache.get(m, [])):
            rows.append([_month_label(m) if i == 0 else "", note])

    if len(rows) == 1:
        rows.append(["", "No anomalies detected."])

    ws.clear()
    ws.update(rows, value_input_option="USER_ENTERED")
    ws.format("A1:B1", {"textFormat": {"bold": True}})


# ── Public API ─────────────────────────────────────────────────────────────────

def export(categorized: list[dict], anomalies: dict) -> None:
    years = sorted({g["month"][:4] for g in categorized})

    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    creds = Google.get_credentials()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)

    expected = {f"Spending {y}" for y in years} | {f"Anomalies {y}" for y in years}

    for ws in sh.worksheets():
        if ws.title not in expected:
            print(f"Removing old tab: {ws.title}")
            sh.del_worksheet(ws)

    def get_or_create(title: str) -> gspread.Worksheet:
        try:
            return sh.worksheet(title)
        except gspread.WorksheetNotFound:
            return sh.add_worksheet(title=title, rows=500, cols=30)

    for year in years:
        year_groups = [g for g in categorized if g["month"].startswith(year)]
        year_months = sorted({g["month"] for g in year_groups})

        print(f"Writing Spending {year}...")
        write_spending_sheet(get_or_create(f"Spending {year}"), year_groups, anomalies)

        print(f"Writing Anomalies {year}...")
        write_anomalies_sheet(get_or_create(f"Anomalies {year}"), anomalies, year_months)

    # Reorder tabs chronologically: Spending {year}, Anomalies {year} for each year
    desired_order = [title for year in years for title in (f"Spending {year}", f"Anomalies {year}")]
    worksheets_by_title = {ws.title: ws for ws in sh.worksheets()}
    sh.reorder_worksheets([worksheets_by_title[t] for t in desired_order if t in worksheets_by_title])

    print(f"Done — https://docs.google.com/spreadsheets/d/{spreadsheet_id}")
