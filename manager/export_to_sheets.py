"""Export the finance summary to a Google Sheet."""
import calendar
import os
import sys
from collections import defaultdict
from datetime import date, datetime

sys.stdout.reconfigure(encoding="utf-8")

import gspread
from dotenv import load_dotenv

from .login import Google

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
        return {"amounts": defaultdict(float), "children": {}, "merchants": defaultdict(list)}

    root = new_node()
    for g in groups:
        path = g.get("path", ["Uncategorized"])
        node = root
        node["amounts"][g["month"]] += g["amount"]
        node["merchants"][g["month"]].append((g["name"], g["amount"]))
        for part in path:
            node["children"].setdefault(part, new_node())
            node = node["children"][part]
            node["amounts"][g["month"]] += g["amount"]
            node["merchants"][g["month"]].append((g["name"], g["amount"]))

    return root, months


def write_spending_sheet(ws: gspread.Worksheet, groups: list[dict], anomaly_cache: dict) -> None:
    root, months = _build_tree(groups)

    month_set = set(months)
    year = months[0][:4]
    all_12 = all(f"{year}-{m:02d}" in month_set for m in range(1, 13))
    total_label = "Total" if all_12 else "Total (partial)"

    col_labels = [_month_label(m) for m in months]

    rows = [["Category"] + col_labels + [total_label]]
    notes: list[tuple[int, int, str]] = []  # (row_0based, col_0based, note_text)

    def _note(entries: list[tuple[str, float]]) -> str:
        sorted_e = sorted(entries, key=lambda x: abs(x[1]), reverse=True)
        return "\n".join(f"* ${abs(a):,.2f}: {n}" for n, a in sorted_e)

    def add_rows(node: dict, depth: int) -> None:
        indent = "  " * depth
        for name, child in sorted(node["children"].items()):
            if not any(child["amounts"].get(m, 0.0) for m in months):
                continue
            row_idx = len(rows)
            row = [indent + name]
            year_total = 0.0
            year_merchants: dict[str, float] = defaultdict(float)
            for col_idx, m in enumerate(months, start=1):
                amt = -child["amounts"].get(m, 0.0)
                year_total += amt
                row.append(round(amt, 2) if amt else "")
                if amt:
                    m_merchants = child["merchants"].get(m, [])
                    if m_merchants:
                        notes.append((row_idx, col_idx, _note(m_merchants)))
                    for mname, mamt in m_merchants:
                        year_merchants[mname] += mamt
            row.append(round(year_total, 2))
            if year_total and year_merchants:
                notes.append((row_idx, len(months) + 1, _note(list(year_merchants.items()))))
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

    # Add merchant breakdown notes (clear first, then set)
    note_requests: list[dict] = [{
        "repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": last_row, "startColumnIndex": 0, "endColumnIndex": n_cols},
            "cell": {"note": ""},
            "fields": "note",
        }
    }]
    for r, c, note_text in notes:
        note_requests.append({
            "updateCells": {
                "range": {"sheetId": ws.id, "startRowIndex": r, "endRowIndex": r + 1, "startColumnIndex": c, "endColumnIndex": c + 1},
                "rows": [{"values": [{"note": note_text}]}],
                "fields": "note",
            }
        })
    ws.spreadsheet.batch_update({"requests": note_requests})


def write_summary_sheet(
    ws: gspread.Worksheet,
    year_groups: list[dict],
    prev_year_groups: list[dict],
    anomaly_cache: dict,
    year: str,
) -> None:
    """Write a summary tab: year-over-year totals by top-level category, then anomalies."""

    def top_totals(groups: list[dict]) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for g in groups:
            top = g.get("path", ["Uncategorized"])[0]
            totals[top] += g["amount"]
        return totals

    curr = top_totals(year_groups)
    prev = top_totals(prev_year_groups)
    has_prev = bool(prev_year_groups)
    prev_year = str(int(year) - 1)

    all_cats = sorted(set(curr) | set(prev))

    # ── Year summary table ────────────────────────────────────────────────────
    if has_prev:
        header = ["Category", prev_year, year, "Change ($)", "Change (%)"]
    else:
        header = ["Category", year]

    rows: list[list] = [header]

    for cat in all_cats:
        c = round(-curr.get(cat, 0.0), 2)
        p = round(-prev.get(cat, 0.0), 2) if has_prev else None
        if has_prev:
            chg = round(c - p, 2)
            pct = round((c - p) / p * 100, 1) if p else ""
            rows.append([cat, p or "", c or "", chg or "", pct])
        else:
            rows.append([cat, c or ""])

    # Total row
    c_total = round(-sum(curr.values()), 2)
    if has_prev:
        p_total = round(-sum(prev.values()), 2)
        chg = round(c_total - p_total, 2)
        pct = round((c_total - p_total) / p_total * 100, 1) if p_total else ""
        rows.append(["Total", p_total, c_total, chg, pct])
    else:
        rows.append(["Total", c_total])

    n_summary_rows = len(rows)  # used for formatting below

    # ── Anomalies section ─────────────────────────────────────────────────────
    year_months = sorted({g["month"] for g in year_groups})
    noted = [(m, anomaly_cache.get(m, [])) for m in year_months if anomaly_cache.get(m)]

    rows.append([""])  # blank separator
    rows.append(["Spending Anomalies"])
    rows.append(["Month", "Anomaly"])
    if noted:
        for m, notes in noted:
            for i, note in enumerate(notes):
                rows.append([_month_label(m, include_year=False) if i == 0 else "", note])
    else:
        rows.append(["", "No anomalies detected."])

    # ── Write & format ────────────────────────────────────────────────────────
    ws.clear()
    ws.update(rows, value_input_option="USER_ENTERED")

    n_cols = len(header)
    last_col_letter = chr(ord("A") + n_cols - 1)

    # Bold header and total row
    ws.format(f"A1:{last_col_letter}1", {"textFormat": {"bold": True}})
    ws.format(f"A{n_summary_rows}:{last_col_letter}{n_summary_rows}", {"textFormat": {"bold": True}})

    # Bold anomaly section header
    anomaly_header_row = n_summary_rows + 3
    ws.format(f"A{anomaly_header_row}:B{anomaly_header_row}", {"textFormat": {"bold": True}})

    # Currency format for numeric columns (skip category column A)
    if n_cols > 1:
        num_range = f"B2:{last_col_letter}{n_summary_rows}"
        ws.format(num_range, {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}})

    ws.freeze(rows=1, cols=1)


# ── Public API ─────────────────────────────────────────────────────────────────

def export(categorized: list[dict], anomalies: dict) -> None:
    years = sorted({g["month"][:4] for g in categorized})

    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    creds = Google.get_credentials()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)

    expected = {f"Spending {y}" for y in years} | {f"Summary {y}" for y in years}

    def get_or_create(title: str) -> gspread.Worksheet:
        try:
            return sh.worksheet(title)
        except gspread.WorksheetNotFound:
            return sh.add_worksheet(title=title, rows=500, cols=30)

    # Write expected tabs first so there is always at least one tab before any deletions
    for year in years:
        year_groups = [g for g in categorized if g["month"].startswith(year)]
        prev_year_groups = [g for g in categorized if g["month"].startswith(str(int(year) - 1))]

        print(f"Writing Spending {year}...")
        write_spending_sheet(get_or_create(f"Spending {year}"), year_groups, anomalies)

        print(f"Writing Summary {year}...")
        write_summary_sheet(get_or_create(f"Summary {year}"), year_groups, prev_year_groups, anomalies, year)

    # Now safe to remove stale tabs — expected tabs already exist
    for ws in sh.worksheets():
        if ws.title not in expected:
            print(f"Removing old tab: {ws.title}")
            sh.del_worksheet(ws)

    # Reorder tabs chronologically: Spending {year}, Summary {year} for each year
    desired_order = [title for year in years for title in [f"Spending {year}", f"Summary {year}"]]
    worksheets_by_title = {ws.title: ws for ws in sh.worksheets()}
    sh.reorder_worksheets([worksheets_by_title[t] for t in desired_order if t in worksheets_by_title])

    print(f"Done — https://docs.google.com/spreadsheets/d/{spreadsheet_id}")
