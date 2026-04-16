"""Tests for transaction filtering and grouping logic."""
import pytest
from manager.transactions.filters import filter_by_date, filter_transactions
from manager.transactions.grouping import group_by_month_merchant


# ── filter_transactions ────────────────────────────────────────────────────────

def _t(date, name, amount):
    return {"date": date, "name": name, "amount": amount, "category": "Test", "id": ""}


def test_filter_removes_dust():
    txns = [_t("2024-01-01", "Coffee", 0.05), _t("2024-01-01", "Groceries", 50.00)]
    result = filter_transactions(txns)
    assert len(result) == 1
    assert result[0]["name"] == "Groceries"


def test_filter_keeps_exactly_threshold():
    txns = [_t("2024-01-01", "A", 0.10)]
    assert len(filter_transactions(txns)) == 1


def test_filter_removes_paired_transfers():
    # Opposite signs, same amount, within 7 days
    txns = [
        _t("2024-01-01", "Transfer Out", -500.00),
        _t("2024-01-03", "Transfer In", 500.00),
    ]
    result = filter_transactions(txns)
    assert result == []


def test_filter_keeps_transfers_outside_window():
    txns = [
        _t("2024-01-01", "Transfer Out", -500.00),
        _t("2024-01-15", "Transfer In", 500.00),
    ]
    result = filter_transactions(txns)
    assert len(result) == 2


def test_filter_keeps_same_sign_same_amount():
    # Two identical expenses are NOT a transfer pair
    txns = [
        _t("2024-01-01", "Netflix", -15.99),
        _t("2024-01-02", "Netflix", -15.99),
    ]
    result = filter_transactions(txns)
    assert len(result) == 2


def test_filter_removes_only_one_pair():
    txns = [
        _t("2024-01-01", "Transfer Out", -200.00),
        _t("2024-01-02", "Transfer In", 200.00),
        _t("2024-01-03", "Groceries", 80.00),
    ]
    result = filter_transactions(txns)
    assert len(result) == 1
    assert result[0]["name"] == "Groceries"


# ── filter_by_date ─────────────────────────────────────────────────────────────

def test_filter_by_date_keeps_recent():
    from datetime import date, timedelta
    today = date.today()
    recent = (today - timedelta(days=30)).isoformat()
    old = (today - timedelta(days=400)).isoformat()
    txns = [
        _t(recent, "Recent", 10.00),
        _t(old, "Old", 10.00),
    ]
    result = filter_by_date(txns, days=365)
    assert len(result) == 1
    assert result[0]["name"] == "Recent"


def test_filter_by_date_keeps_all_within_window():
    from datetime import date, timedelta
    today = date.today()
    txns = [_t((today - timedelta(days=i * 10)).isoformat(), f"T{i}", 10.00) for i in range(5)]
    result = filter_by_date(txns, days=365)
    assert len(result) == 5


# ── group_by_month_merchant ────────────────────────────────────────────────────

def test_group_aggregates_same_merchant_same_month():
    txns = [
        _t("2024-01-05", "Starbucks", -5.00),
        _t("2024-01-20", "Starbucks", -6.00),
    ]
    groups = group_by_month_merchant(txns)
    assert len(groups) == 1
    assert groups[0]["month"] == "2024-01"
    assert groups[0]["name"] == "Starbucks"
    assert groups[0]["amount"] == pytest.approx(-11.00)
    assert groups[0]["count"] == 2


def test_group_separates_different_months():
    txns = [
        _t("2024-01-05", "Starbucks", -5.00),
        _t("2024-02-10", "Starbucks", -6.00),
    ]
    groups = group_by_month_merchant(txns)
    assert len(groups) == 2
    months = {g["month"] for g in groups}
    assert months == {"2024-01", "2024-02"}


def test_group_separates_different_merchants():
    txns = [
        _t("2024-01-01", "Starbucks", -5.00),
        _t("2024-01-02", "Netflix", -15.99),
    ]
    groups = group_by_month_merchant(txns)
    assert len(groups) == 2
    names = {g["name"] for g in groups}
    assert names == {"Starbucks", "Netflix"}


def test_group_output_sorted():
    txns = [
        _t("2024-03-01", "Z-Merchant", -10.00),
        _t("2024-01-01", "A-Merchant", -20.00),
    ]
    groups = group_by_month_merchant(txns)
    assert groups[0]["month"] == "2024-01"
    assert groups[1]["month"] == "2024-03"
