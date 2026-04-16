"""Tests for manager/sheets/summary.py — prompt builder, batch insight generation, and sheet writing."""
from unittest.mock import MagicMock, patch

import gspread
import pytest

from manager.sheets.summary import _build_summary_prompt, generate_all_summary_insights, write_summary_sheet


# ── Sample data ────────────────────────────────────────────────────────────────

def _groups(year: str, months=("01",), amount=-500.0, top="Food") -> list[dict]:
    return [
        {"month": f"{year}-{m}", "amount": amount, "path": [top, "Groceries"]}
        for m in months
    ]


# ── _build_summary_prompt ──────────────────────────────────────────────────────

def test_build_summary_prompt_returns_string():
    groups = _groups("2024")
    prompt = _build_summary_prompt(groups, [], "2024")
    assert isinstance(prompt, str)
    assert len(prompt) > 50


def test_build_summary_prompt_contains_year():
    groups = _groups("2024")
    prompt = _build_summary_prompt(groups, [], "2024")
    assert "2024" in prompt


def test_build_summary_prompt_includes_category_totals():
    groups = _groups("2024", months=("01", "02"), amount=-300.0, top="Housing")
    prompt = _build_summary_prompt(groups, [], "2024")
    # Category name and total (600) should appear
    assert "Housing" in prompt
    assert "600" in prompt


def test_build_summary_prompt_yoy_comparison_when_prev_provided():
    curr = _groups("2024", months=("01",), amount=-800.0, top="Travel")
    prev = _groups("2023", months=("01",), amount=-400.0, top="Travel")
    prompt = _build_summary_prompt(curr, prev, "2024")
    assert "2023" in prompt
    assert "2024" in prompt
    # Should mention year-over-year change
    assert "Travel" in prompt


def test_build_summary_prompt_no_prev_year_single_column():
    groups = _groups("2024")
    prompt = _build_summary_prompt(groups, [], "2024")
    # Without prev year, no YoY table
    assert "2023" not in prompt


def test_build_summary_prompt_asks_for_insights():
    groups = _groups("2024")
    prompt = _build_summary_prompt(groups, [], "2024")
    # Should ask for insights/observations
    assert "insight" in prompt.lower() or "spending" in prompt.lower()


# ── generate_all_summary_insights ─────────────────────────────────────────────

def test_generate_all_summary_insights_returns_dict_keyed_by_year():
    years_data = [
        ("2023", _groups("2023"), []),
        ("2024", _groups("2024"), _groups("2023")),
    ]
    mock_results = {
        "2023": {"insights": ["Spent $6,000 on Food in 2023."]},
        "2024": {"insights": ["Food up 20% year-over-year.", "Housing stable."]},
    }
    with patch("manager.sheets.summary.llm_batch_structured", return_value=mock_results):
        out = generate_all_summary_insights(years_data)

    assert set(out.keys()) == {"2023", "2024"}
    assert out["2023"] == ["Spent $6,000 on Food in 2023."]
    assert len(out["2024"]) == 2


def test_generate_all_summary_insights_calls_batch_with_all_years():
    years_data = [
        ("2022", _groups("2022"), []),
        ("2023", _groups("2023"), _groups("2022")),
        ("2024", _groups("2024"), _groups("2023")),
    ]
    captured = {}

    def fake_batch(prompts, schema, tag):
        captured["prompts"] = prompts
        return {year: {"insights": []} for year, _ in prompts}

    with patch("manager.sheets.summary.llm_batch_structured", side_effect=fake_batch), \
         patch("manager.sheets.summary.sheets_cache.read_cache", return_value={}), \
         patch("manager.sheets.summary.sheets_cache.write_cache"):
        generate_all_summary_insights(years_data)

    # Should have sent all 3 years in a single batch call
    assert len(captured["prompts"]) == 3
    years_sent = [y for y, _ in captured["prompts"]]
    assert years_sent == ["2022", "2023", "2024"]


def test_generate_all_summary_insights_uses_cache_for_past_years():
    """Cached past years should not trigger LLM calls."""
    years_data = [
        ("2022", _groups("2022"), []),
        ("2023", _groups("2023"), _groups("2022")),
    ]
    existing_cache = {"2022": ["Food spending was $6,000."]}
    captured = {}

    def fake_batch(prompts, schema, tag):
        captured["prompts"] = prompts
        return {year: {"insights": ["New insight."]} for year, _ in prompts}

    with patch("manager.sheets.summary.llm_batch_structured", side_effect=fake_batch), \
         patch("manager.sheets.summary.sheets_cache.read_cache", return_value=existing_cache), \
         patch("manager.sheets.summary.sheets_cache.write_cache"):
        out = generate_all_summary_insights(years_data)

    # Only 2023 should be sent to LLM; 2022 served from cache
    assert captured.get("prompts") is not None
    years_sent = [y for y, _ in captured["prompts"]]
    assert "2022" not in years_sent
    assert "2023" in years_sent
    # 2022 result should come from cache
    assert out["2022"] == ["Food spending was $6,000."]


def test_generate_all_summary_insights_writes_only_past_years_to_cache():
    """Current year should not be written to cache."""
    from datetime import date
    current_year = date.today().strftime("%Y")
    past_year = str(int(current_year) - 1)

    years_data = [
        (past_year, _groups(past_year), []),
        (current_year, _groups(current_year), _groups(past_year)),
    ]
    written = {}

    def fake_write(tab, data):
        written.update(data)

    with patch("manager.sheets.summary.llm_batch_structured",
               return_value={past_year: {"insights": ["Past insight."]},
                             current_year: {"insights": ["Current insight."]}}), \
         patch("manager.sheets.summary.sheets_cache.read_cache", return_value={}), \
         patch("manager.sheets.summary.sheets_cache.write_cache", side_effect=fake_write):
        generate_all_summary_insights(years_data)

    assert past_year in written
    assert current_year not in written


def test_generate_all_summary_insights_skips_llm_when_all_cached():
    """No LLM call should be made when all past years are already cached."""
    years_data = [("2022", _groups("2022"), []), ("2023", _groups("2023"), _groups("2022"))]
    cache = {"2022": ["Insight A."], "2023": ["Insight B."]}

    with patch("manager.sheets.summary.llm_batch_structured") as mock_llm, \
         patch("manager.sheets.summary.sheets_cache.read_cache", return_value=cache), \
         patch("manager.sheets.summary.sheets_cache.write_cache"):
        out = generate_all_summary_insights(years_data)

    mock_llm.assert_not_called()
    assert out == {"2022": ["Insight A."], "2023": ["Insight B."]}


def test_generate_all_summary_insights_missing_year_returns_empty_list():
    """If the LLM omits a year from results, return an empty list for that year."""
    years_data = [("2024", _groups("2024"), [])]
    with patch("manager.sheets.summary.llm_batch_structured", return_value={}):
        out = generate_all_summary_insights(years_data)
    assert out == {"2024": []}


def test_generate_all_summary_insights_single_year():
    years_data = [("2024", _groups("2024"), [])]
    mock_results = {"2024": {"insights": ["Total spending was $500."]}}
    with patch("manager.sheets.summary.llm_batch_structured", return_value=mock_results):
        out = generate_all_summary_insights(years_data)
    assert out["2024"] == ["Total spending was $500."]


# ── write_summary_sheet: resize ───────────────────────────────────────────────

def _summary_ws() -> MagicMock:
    ws = MagicMock(spec=gspread.Worksheet)
    ws.id = 1
    ws.spreadsheet = MagicMock()
    return ws


def test_write_summary_sheet_calls_resize():
    """ws.resize() must be called once with rows > 0 and cols == 2."""
    ws = _summary_ws()
    groups = _groups("2024", months=("01", "02"))
    write_summary_sheet(ws, groups, [], {}, "2024", ["Insight 1.", "Insight 2."])
    ws.resize.assert_called_once()
    kwargs = ws.resize.call_args.kwargs
    assert kwargs["cols"] == 2
    assert isinstance(kwargs["rows"], int) and kwargs["rows"] > 0


def test_write_summary_sheet_resize_before_update():
    """resize() must be called after clear() and before update()."""
    ws = _summary_ws()
    groups = _groups("2024")
    write_summary_sheet(ws, groups, [], {}, "2024", [])
    method_names = [c[0] for c in ws.mock_calls]
    assert method_names.index("clear") < method_names.index("resize")
    assert method_names.index("resize") < method_names.index("update")
