"""Tests for manager/anomaly.py — dominant merchant detection and prompt building."""
from unittest.mock import patch

import pytest

from manager.anomaly import _dominant_merchants, _monthly_totals_by_top_cat, detect_monthly_anomalies


# ── Helpers ────────────────────────────────────────────────────────────────────

def _group(month: str, name: str, amount: float, path: list[str]) -> dict:
    return {"month": month, "name": name, "amount": amount, "path": path}


# ── _dominant_merchants ────────────────────────────────────────────────────────

def test_dominant_merchants_single_dominant():
    groups = [
        _group("2024-01", "Delta Airlines", -800.0, ["Travel"]),
        _group("2024-01", "Uber",            -200.0, ["Travel"]),
    ]
    totals = _monthly_totals_by_top_cat(groups)
    result = _dominant_merchants(groups, totals)
    assert "2024-01" in result
    names = [r[0] for r in result["2024-01"]]
    assert "Delta Airlines" in names   # 80% of Travel — dominant
    assert "Uber" in names             # 20% exactly — at threshold, so included
    # Check percentage is correct
    delta = next(r for r in result["2024-01"] if r[0] == "Delta Airlines")
    assert delta[2] == pytest.approx(800.0)
    assert delta[3] == pytest.approx(0.80)


def test_dominant_merchants_threshold_exactly_20_percent():
    """A merchant at exactly 20% should be included."""
    groups = [
        _group("2024-01", "Amazon", -200.0, ["Shopping"]),
        _group("2024-01", "Other",  -800.0, ["Shopping"]),
    ]
    totals = _monthly_totals_by_top_cat(groups)
    result = _dominant_merchants(groups, totals)
    names = [r[0] for r in result.get("2024-01", [])]
    assert "Amazon" in names


def test_dominant_merchants_below_threshold_excluded():
    """A merchant below 20% should not appear."""
    groups = [
        _group("2024-01", "Starbucks", -50.0,  ["Food"]),
        _group("2024-01", "Groceries", -950.0, ["Food"]),
    ]
    totals = _monthly_totals_by_top_cat(groups)
    result = _dominant_merchants(groups, totals)
    names = [r[0] for r in result.get("2024-01", [])]
    assert "Starbucks" not in names   # 5% < 20%
    assert "Groceries" in names       # 95% > 20%


def test_dominant_merchants_sorted_by_spend_descending():
    groups = [
        _group("2024-01", "Small",  -300.0, ["Travel"]),
        _group("2024-01", "Large",  -700.0, ["Travel"]),
    ]
    totals = _monthly_totals_by_top_cat(groups)
    result = _dominant_merchants(groups, totals)
    names = [r[0] for r in result["2024-01"]]
    assert names[0] == "Large"
    assert names[1] == "Small"


def test_dominant_merchants_zero_category_total_skipped():
    """Groups with zero category total should not cause division by zero."""
    groups = [_group("2024-01", "Refund", 0.0, ["Shopping"])]
    totals = _monthly_totals_by_top_cat(groups)
    result = _dominant_merchants(groups, totals)
    assert result.get("2024-01", []) == []


def test_dominant_merchants_multiple_months_independent():
    groups = [
        _group("2024-01", "Delta", -800.0, ["Travel"]),
        _group("2024-01", "Uber",  -200.0, ["Travel"]),
        _group("2024-02", "Hotel", -600.0, ["Travel"]),
        _group("2024-02", "Train", -400.0, ["Travel"]),
    ]
    totals = _monthly_totals_by_top_cat(groups)
    result = _dominant_merchants(groups, totals)
    jan_names = [r[0] for r in result["2024-01"]]
    feb_names = [r[0] for r in result["2024-02"]]
    assert "Delta" in jan_names
    assert "Hotel" in feb_names
    assert "Delta" not in feb_names


def test_dominant_merchants_no_path_uses_uncategorized():
    groups = [{"month": "2024-01", "name": "Mystery", "amount": -500.0, "path": []}]
    totals = _monthly_totals_by_top_cat(groups)
    result = _dominant_merchants(groups, totals)
    # Should not crash; "Uncategorized" category total is 500
    entry = result.get("2024-01", [])
    assert any(r[0] == "Mystery" for r in entry)


# ── Prompt includes merchant hints ─────────────────────────────────────────────

def _make_groups_for_prompt(year: str = "2022") -> list[dict]:
    """12 months of history + 1 analysis month so detect_monthly_anomalies has data."""
    groups = []
    for i in range(1, 13):
        m = f"{year}-{i:02d}"
        groups.append(_group(m, "Grocery Store", -400.0, ["Food"]))
        groups.append(_group(m, "Electric Co",   -100.0, ["Utilities"]))
    # Analysis month: big single transaction dominates Food
    groups.append(_group(f"{int(year)+1}-01", "Whole Foods",  -900.0, ["Food"]))
    groups.append(_group(f"{int(year)+1}-01", "Coffee Shop",  -100.0, ["Food"]))
    groups.append(_group(f"{int(year)+1}-01", "Electric Co",  -100.0, ["Utilities"]))
    return groups


def test_detect_monthly_anomalies_prompt_includes_dominant_merchant(tmp_path):
    """When a merchant ≥20% of its category, its name appears in the LLM prompt."""
    groups = _make_groups_for_prompt()
    captured_prompts = {}

    def fake_batch(prompts, schema, tag):
        for month, prompt in prompts:
            captured_prompts[month] = prompt
        return {month: {"anomalies": []} for month, _ in prompts}

    with patch("manager.sheets_cache.read_cache", return_value={}), \
         patch("manager.sheets_cache.write_cache"), \
         patch("manager.anomaly.llm_batch_structured", side_effect=fake_batch):
        detect_monthly_anomalies(groups)

    # The analysis month should have been prompted
    assert len(captured_prompts) == 1
    prompt_text = list(captured_prompts.values())[0]
    assert "Whole Foods" in prompt_text
    assert "≥20%" in prompt_text or "20%" in prompt_text


def test_detect_monthly_anomalies_prompt_omits_hint_when_no_dominant(tmp_path):
    """When no merchant reaches 20%, the hint section is omitted from the prompt."""
    groups = []
    for i in range(1, 13):
        m = f"2022-{i:02d}"
        groups.append(_group(m, "A", -500.0, ["Food"]))
        groups.append(_group(m, "B", -500.0, ["Food"]))
    # Analysis month: 6 equal merchants each at ~16.7% — all below 20% threshold
    for j in range(1, 7):
        groups.append(_group("2023-01", f"M{j}", -100.0, ["Food"]))

    captured_prompts = {}

    def fake_batch(prompts, schema, tag):
        for month, prompt in prompts:
            captured_prompts[month] = prompt
        return {month: {"anomalies": []} for month, _ in prompts}

    with patch("manager.sheets_cache.read_cache", return_value={}), \
         patch("manager.sheets_cache.write_cache"), \
         patch("manager.anomaly.llm_batch_structured", side_effect=fake_batch):
        detect_monthly_anomalies(groups)

    prompt_text = list(captured_prompts.values())[0]
    assert "Single transactions" not in prompt_text
