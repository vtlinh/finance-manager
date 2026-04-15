"""Google Sheets-backed cache stored in hidden worksheet tabs.

Each cache is serialized as a single JSON string in cell A1 of a dedicated
hidden tab (e.g. ``_merchant_cache``, ``_anomaly_cache``).  All reads and
writes are no-ops when Google credentials or a spreadsheet ID are not
available, so CLI dev runs degrade gracefully to an empty cache.
"""
import json
import os
from typing import Optional

_sh_cache: dict = {}  # spreadsheet_id -> gspread.Spreadsheet


def _get_spreadsheet():
    """Return the configured gspread Spreadsheet, or None if not available."""
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        return None
    if spreadsheet_id in _sh_cache:
        return _sh_cache[spreadsheet_id]
    try:
        import gspread
        from .login import Google
        creds = Google.get_credentials()
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(spreadsheet_id)
        _sh_cache[spreadsheet_id] = sh
        return sh
    except Exception as exc:
        print(f"Warning: could not open spreadsheet for cache ({exc})")
        return None


def read_cache(tab_name: str) -> dict:
    """Read a JSON dict from cell A1 of *tab_name*.  Returns {} if absent."""
    sh = _get_spreadsheet()
    if sh is None:
        return {}
    try:
        ws = next((w for w in sh.worksheets() if w.title == tab_name), None)
        if ws is None:
            return {}
        value = ws.acell("A1").value
        return json.loads(value) if value else {}
    except Exception as exc:
        print(f"Warning: could not read cache from '{tab_name}' ({exc})")
        return {}


def write_cache(tab_name: str, data: dict) -> None:
    """Write *data* as JSON to cell A1 of *tab_name*, creating the hidden tab if needed."""
    sh = _get_spreadsheet()
    if sh is None:
        return
    try:
        ws = next((w for w in sh.worksheets() if w.title == tab_name), None)
        if ws is None:
            ws = sh.add_worksheet(title=tab_name, rows=1, cols=1)
            ws.hide()
        ws.update_cell(1, 1, json.dumps(data, sort_keys=True))
    except Exception as exc:
        print(f"Warning: could not write cache to '{tab_name}' ({exc})")
