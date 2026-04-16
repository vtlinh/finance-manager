"""Tests for manager/sheets/writer.py — tab management and export orchestration."""
from unittest.mock import MagicMock, call, patch

import pytest
import gspread

from manager.sheets.writer import export


def _mock_worksheet(title: str) -> MagicMock:
    ws = MagicMock(spec=gspread.Worksheet)
    ws.title = title
    ws.id = hash(title)
    return ws


def _categorized(year: str) -> list[dict]:
    return [
        {"month": f"{year}-01", "amount": -500.0, "path": ["Food", "Groceries"],
         "merchant": "Whole Foods", "transactions": []},
    ]


def _make_spreadsheet(existing_tabs: list[str]) -> MagicMock:
    sh = MagicMock()
    worksheets = [_mock_worksheet(t) for t in existing_tabs]
    sh.worksheets.return_value = worksheets

    def get_or_create(title: str):
        for ws in worksheets:
            if ws.title == title:
                return ws
        new_ws = _mock_worksheet(title)
        worksheets.append(new_ws)
        return new_ws

    def worksheet(title: str):
        for ws in worksheets:
            if ws.title == title:
                return ws
        raise gspread.WorksheetNotFound(title)

    sh.worksheet.side_effect = worksheet
    sh.add_worksheet.side_effect = lambda title, rows, cols: get_or_create(title)
    return sh


# ── Cache tab hiding ───────────────────────────────────────────────────────────

def test_export_hides_cache_tabs_not_deletes():
    """Tabs whose names start with '_' must be hidden, not deleted."""
    categorized = _categorized("2024")
    sh = _make_spreadsheet(["Spending 2024", "Summary 2024", "_merchant_cache", "_anomaly_cache"])

    mock_creds = MagicMock()
    mock_gc = MagicMock()
    mock_gc.open_by_key.return_value = sh

    with patch("os.environ", {"SPREADSHEET_ID": "fake_id"}), \
         patch("manager.sheets.writer.Google.get_credentials", return_value=mock_creds), \
         patch("gspread.authorize", return_value=mock_gc), \
         patch("manager.sheets.writer.write_spending_sheet"), \
         patch("manager.sheets.writer.write_summary_sheet"), \
         patch("manager.sheets.writer.generate_all_summary_insights", return_value={"2024": []}):
        export(categorized, {})

    # Cache tabs should be hidden
    for ws in sh.worksheets():
        if ws.title.startswith("_"):
            ws.hide.assert_called_once()
            sh.del_worksheet.assert_not_called()


def test_export_deletes_stale_non_cache_tabs():
    """Tabs that are neither expected nor cache tabs should be deleted."""
    categorized = _categorized("2024")
    sh = _make_spreadsheet(["Spending 2024", "Summary 2024", "Spending 2022", "Old Tab"])

    mock_creds = MagicMock()
    mock_gc = MagicMock()
    mock_gc.open_by_key.return_value = sh

    deleted_titles = []
    sh.del_worksheet.side_effect = lambda ws: deleted_titles.append(ws.title)

    with patch("os.environ", {"SPREADSHEET_ID": "fake_id"}), \
         patch("manager.sheets.writer.Google.get_credentials", return_value=mock_creds), \
         patch("gspread.authorize", return_value=mock_gc), \
         patch("manager.sheets.writer.write_spending_sheet"), \
         patch("manager.sheets.writer.write_summary_sheet"), \
         patch("manager.sheets.writer.generate_all_summary_insights", return_value={"2024": []}):
        export(categorized, {})

    assert "Spending 2022" in deleted_titles
    assert "Old Tab" in deleted_titles


def test_export_keeps_expected_tabs():
    """Expected tabs for the current year must never be deleted or hidden."""
    categorized = _categorized("2024")
    sh = _make_spreadsheet(["Spending 2024", "Summary 2024"])

    mock_creds = MagicMock()
    mock_gc = MagicMock()
    mock_gc.open_by_key.return_value = sh

    with patch("os.environ", {"SPREADSHEET_ID": "fake_id"}), \
         patch("manager.sheets.writer.Google.get_credentials", return_value=mock_creds), \
         patch("gspread.authorize", return_value=mock_gc), \
         patch("manager.sheets.writer.write_spending_sheet"), \
         patch("manager.sheets.writer.write_summary_sheet"), \
         patch("manager.sheets.writer.generate_all_summary_insights", return_value={"2024": []}):
        export(categorized, {})

    sh.del_worksheet.assert_not_called()
    for ws in sh.worksheets():
        if ws.title in ("Spending 2024", "Summary 2024"):
            ws.hide.assert_not_called()


def test_export_calls_generate_all_summary_insights_once():
    """Batch summary generation should be called exactly once regardless of year count."""
    categorized = _categorized("2023") + _categorized("2024")
    sh = _make_spreadsheet([])

    mock_creds = MagicMock()
    mock_gc = MagicMock()
    mock_gc.open_by_key.return_value = sh

    with patch("os.environ", {"SPREADSHEET_ID": "fake_id"}), \
         patch("manager.sheets.writer.Google.get_credentials", return_value=mock_creds), \
         patch("gspread.authorize", return_value=mock_gc), \
         patch("manager.sheets.writer.write_spending_sheet"), \
         patch("manager.sheets.writer.write_summary_sheet"), \
         patch("manager.sheets.writer.generate_all_summary_insights",
               return_value={"2023": [], "2024": []}) as mock_gen:
        export(categorized, {})

    mock_gen.assert_called_once()
    years_in_call = [y for y, _, _ in mock_gen.call_args[0][0]]
    assert "2023" in years_in_call
    assert "2024" in years_in_call
