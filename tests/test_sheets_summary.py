"""Tests for manager/sheets/summary.py — prompt builder and batch insight generation."""
from unittest.mock import patch

import pytest

from manager.sheets.summary import _build_summary_prompt, generate_all_summary_insights


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

    with patch("manager.sheets.summary.llm_batch_structured", side_effect=fake_batch):
        generate_all_summary_insights(years_data)

    # Should have sent all 3 years in a single batch call
    assert len(captured["prompts"]) == 3
    years_sent = [y for y, _ in captured["prompts"]]
    assert years_sent == ["2022", "2023", "2024"]


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
