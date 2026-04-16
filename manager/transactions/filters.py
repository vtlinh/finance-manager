from datetime import datetime, timedelta


def filter_by_date(transactions: list[dict], days: int = 365) -> list[dict]:
    """Keep only transactions within the last `days` days from today."""
    cutoff = datetime.today() - timedelta(days=days)
    return [
        t for t in transactions
        if datetime.strptime(t["date"], "%Y-%m-%d") >= cutoff
    ]


def filter_transactions(transactions: list[dict], days_window: int = 7, amount_tolerance: float = 0.01) -> list[dict]:
    """Remove dust (< $0.10) and paired transfer transactions (opposite-sign, matching amount within days_window days)."""
    transactions = [t for t in transactions if abs(t["amount"]) >= 0.1]
    removed: set[int] = set()

    for i in range(len(transactions)):
        if i in removed:
            continue
        t1 = transactions[i]
        try:
            d1 = datetime.strptime(t1["date"], "%Y-%m-%d")
        except ValueError:
            continue

        for j in range(i + 1, len(transactions)):
            if j in removed:
                continue
            t2 = transactions[j]

            if not ((t1["amount"] > 0 and t2["amount"] < 0) or (t1["amount"] < 0 and t2["amount"] > 0)):
                continue
            if abs(abs(t1["amount"]) - abs(t2["amount"])) > amount_tolerance:
                continue

            try:
                d2 = datetime.strptime(t2["date"], "%Y-%m-%d")
            except ValueError:
                continue

            if abs((d1 - d2).days) <= days_window:
                removed.add(i)
                removed.add(j)
                break

    return [t for i, t in enumerate(transactions) if i not in removed]
