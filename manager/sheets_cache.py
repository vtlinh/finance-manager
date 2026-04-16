"""Google Sheets-backed cache stored in hidden worksheet tabs.

Each cache is stored as a two-column table (key, JSON-encoded value) in a
dedicated hidden tab.  List values are expanded — one row per list item —
so the sheet never hits the 50 000-character single-cell limit.  All reads
and writes are no-ops when Google credentials or a spreadsheet ID are not
available, so CLI dev runs degrade gracefully to an empty cache.
"""
import json
import os

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
    """Read the cache table from *tab_name*.

    Each row is [key, json_value].  If a key appears more than once the
    values are collected into a list (used by the anomaly cache where each
    month may have several notes).  Returns {} if the tab is absent.
    """
    sh = _get_spreadsheet()
    if sh is None:
        return {}
    try:
        ws = next((w for w in sh.worksheets() if w.title == tab_name), None)
        if ws is None:
            return {}
        rows = ws.get_all_values()
        raw: dict[str, list] = {}
        for row in rows:
            if len(row) >= 2 and row[0]:
                key = row[0]
                try:
                    value = json.loads(row[1])
                except (json.JSONDecodeError, ValueError):
                    value = row[1]
                raw.setdefault(key, []).append(value)
        # Unwrap single-item lists so dict values stay scalar where appropriate
        return {k: v[0] if len(v) == 1 else v for k, v in raw.items()}
    except Exception as exc:
        print(f"Warning: could not read cache from '{tab_name}' ({exc})")
        return {}


def write_cache(tab_name: str, data: dict) -> None:
    """Write *data* to *tab_name* as a two-column key/value table.

    List values are expanded: one row per list item so no single cell ever
    exceeds the Sheets 50 000-character limit.
    """
    sh = _get_spreadsheet()
    if sh is None:
        return
    try:
        rows: list[list] = []
        for key, value in data.items():
            if isinstance(value, list):
                for item in value:
                    rows.append([key, json.dumps(item)])
            else:
                rows.append([key, json.dumps(value, sort_keys=True)])

        ws = next((w for w in sh.worksheets() if w.title == tab_name), None)
        if ws is None:
            ws = sh.add_worksheet(title=tab_name, rows=max(len(rows), 1), cols=2)
            ws.hide()
        else:
            ws.clear()

        if rows:
            ws.update(range_name="A1", values=rows)
    except Exception as exc:
        print(f"Warning: could not write cache to '{tab_name}' ({exc})")
