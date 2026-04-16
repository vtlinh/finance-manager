"""Tests for manager/sheets/spending.py — resize and sheet writing."""
from unittest.mock import MagicMock, call

import gspread
import pytest

from manager.sheets.spending import write_spending_sheet


def _ws() -> MagicMock:
    ws = MagicMock(spec=gspread.Worksheet)
    ws.id = 1
    ws.spreadsheet = MagicMock()
    return ws


def _groups(months=("2024-01",)) -> list[dict]:
    return [
        {"month": m, "amount": -100.0, "path": ["Food", "Groceries"],
         "name": "Whole Foods", "count": 1}
        for m in months
    ]


# ── resize ────────────────────────────────────────────────────────────────────

def test_write_spending_sheet_calls_resize():
    """ws.resize() must be called once with positive integer dimensions."""
    ws = _ws()
    write_spending_sheet(ws, _groups(), {})
    ws.resize.assert_called_once()
    kwargs = ws.resize.call_args.kwargs
    assert isinstance(kwargs["rows"], int) and kwargs["rows"] > 0
    assert isinstance(kwargs["cols"], int) and kwargs["cols"] > 0


def test_write_spending_sheet_resize_cols_matches_months():
    """Number of columns = 1 (Category) + len(months) + 1 (Total)."""
    ws = _ws()
    months = ("2024-01", "2024-02", "2024-03")
    write_spending_sheet(ws, _groups(months), {})
    kwargs = ws.resize.call_args.kwargs
    # 1 category col + 3 month cols + 1 total col = 5
    assert kwargs["cols"] == len(months) + 2


def test_write_spending_sheet_resize_before_update():
    """resize() must be called after clear() and before update()."""
    ws = _ws()
    write_spending_sheet(ws, _groups(), {})
    method_names = [c[0] for c in ws.mock_calls]
    assert "clear" in method_names
    assert "resize" in method_names
    assert "update" in method_names
    assert method_names.index("clear") < method_names.index("resize")
    assert method_names.index("resize") < method_names.index("update")
