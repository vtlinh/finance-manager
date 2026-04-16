"""Tests for manager/sheets_cache.py — write_cache resize behaviour."""
from unittest.mock import MagicMock, patch

from manager.sheets_cache import write_cache


def _make_sh(existing_ws: MagicMock | None = None) -> MagicMock:
    sh = MagicMock()
    worksheets = [existing_ws] if existing_ws else []
    sh.worksheets.return_value = worksheets
    return sh


def _existing_ws(title: str) -> MagicMock:
    ws = MagicMock()
    ws.title = title
    return ws


# ── resize ────────────────────────────────────────────────────────────────────

def test_write_cache_resizes_existing_tab():
    """Existing cache tab must be resized to match the written data."""
    ws = _existing_ws("_test_cache")
    sh = _make_sh(ws)
    data = {"k1": "v1", "k2": "v2"}  # 2 rows

    with patch("manager.sheets_cache._get_spreadsheet", return_value=sh):
        write_cache("_test_cache", data)

    ws.resize.assert_called_once_with(rows=2, cols=2)


def test_write_cache_resizes_new_tab():
    """Newly created cache tab must also be resized."""
    sh = _make_sh()
    new_ws = MagicMock()
    new_ws.title = "_new_cache"
    sh.add_worksheet.return_value = new_ws  # no side_effect set, so return_value is used
    data = {"a": [1, 2, 3]}  # list value → 3 rows

    with patch("manager.sheets_cache._get_spreadsheet", return_value=sh):
        write_cache("_new_cache", data)

    new_ws.resize.assert_called_once_with(rows=3, cols=2)


def test_write_cache_resize_minimum_one_row_when_empty():
    """Empty data must resize to 1 row (not 0) to keep the tab valid."""
    ws = _existing_ws("_empty_cache")
    sh = _make_sh(ws)

    with patch("manager.sheets_cache._get_spreadsheet", return_value=sh):
        write_cache("_empty_cache", {})

    ws.resize.assert_called_once_with(rows=1, cols=2)


def test_write_cache_resize_before_update():
    """resize() must be called before update()."""
    ws = _existing_ws("_test_cache")
    sh = _make_sh(ws)
    data = {"x": "y"}

    with patch("manager.sheets_cache._get_spreadsheet", return_value=sh):
        write_cache("_test_cache", data)

    method_names = [c[0] for c in ws.mock_calls]
    assert "resize" in method_names
    assert "update" in method_names
    assert method_names.index("resize") < method_names.index("update")
