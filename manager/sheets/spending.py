import gspread

from .helpers import _month_label, retry_on_quota
from .tree import _build_tree


def print_spreadsheet(groups: list[dict], anomaly_cache: dict) -> None:
    root, months = _build_tree(groups)

    CAT_W  = 34
    COL_W  = 9
    TOT_W  = 12
    INDENT = 2

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
        else:
            amt = sum(node["amounts"].get(m, 0.0) for m in months if m.startswith(val + "-"))
        return -amt if negate else amt

    header = "Category".ljust(CAT_W)
    for lbl, w in zip(col_labels, col_widths):
        header += lbl.rjust(w)
    sep = "─" * len(header)

    print(sep)
    print(header)
    print(sep)

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

    print(sep)
    total_row = "Total".ljust(CAT_W)
    for col, w in zip(cols, col_widths):
        total_row += fmt_amt(col_amt(root, col, negate=False)).rjust(w)
    print(total_row)
    print(sep)

    noted = [(m, anomaly_cache[m]) for m in months if anomaly_cache.get(m)]
    if noted:
        print()
        print("Spending Anomalies")
        print("─" * 50)
        for m, notes in noted:
            print(f"\n  {_month_label(m, include_year=True)} ({m}):")
            for note in notes:
                print(f"    • {note}")


@retry_on_quota
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
            year_merchants: dict[str, float] = {}
            for col_idx, m in enumerate(months, start=1):
                amt = -child["amounts"].get(m, 0.0)
                year_total += amt
                row.append(round(amt, 2) if amt else "")
                if amt:
                    m_merchants = child["merchants"].get(m, [])
                    if m_merchants:
                        notes.append((row_idx, col_idx, _note(m_merchants)))
                    for mname, mamt in m_merchants:
                        year_merchants[mname] = year_merchants.get(mname, 0.0) + mamt
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

    n_cols = len(rows[0])
    last_row = len(rows)

    ws.clear()
    ws.resize(rows=last_row, cols=n_cols)
    ws.update(rows, value_input_option="USER_ENTERED")

    a1_header = gspread.utils.rowcol_to_a1(1, n_cols)
    a1_total = gspread.utils.rowcol_to_a1(last_row, n_cols)
    ws.format(f"A1:{a1_header}", {"textFormat": {"bold": True}})
    ws.format(f"A{last_row}:{a1_total}", {"textFormat": {"bold": True}})

    a1_nums = f"B2:{gspread.utils.rowcol_to_a1(last_row, n_cols)}"
    ws.format(a1_nums, {"numberFormat": {"type": "CURRENCY", "pattern": "$#,##0.00"}})

    ws.freeze(rows=1, cols=1)

    ws.spreadsheet.batch_update({"requests": [
        {"autoResizeDimensions": {"dimensions": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": n_cols}}},
        {"autoResizeDimensions": {"dimensions": {"sheetId": ws.id, "dimension": "ROWS", "startIndex": 0, "endIndex": last_row}}},
    ]})

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
