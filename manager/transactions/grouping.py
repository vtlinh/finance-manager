from collections import defaultdict


def group_by_month_merchant(transactions: list[dict]) -> list[dict]:
    """Aggregate transactions by (month, merchant), summing amounts."""
    groups: dict[tuple, dict] = defaultdict(lambda: {"amount": 0.0, "count": 0, "category": "Uncategorized"})

    for t in transactions:
        month = t["date"][:7]  # "YYYY-MM"
        key = (month, t["name"])
        groups[key]["amount"] += t["amount"]
        groups[key]["count"] += 1
        groups[key]["category"] = t["category"]

    return [
        {"month": month, "name": name, **data}
        for (month, name), data in sorted(groups.items())
    ]
