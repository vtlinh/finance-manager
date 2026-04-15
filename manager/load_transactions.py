import csv
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from .login import Monarch

_DIR = Path(__file__).parent
MONARCH_LIMIT = 100000


def _parse_date(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()


def _make_item(t: dict) -> dict:
    return {
        "id": t.get("id", ""),
        "date": t.get("date", ""),
        "name": t.get("merchant", {}).get("name", t.get("plaidName", "Unknown")),
        "amount": t.get("amount", 0),
        "category": t.get("category", {}).get("name", "Uncategorized"),
    }


# ── CSV ────────────────────────────────────────────────────────────────────────

def load_from_csv(path: Path) -> list[dict]:
    items = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            items.append({
                "date": row.get("Date", ""),
                "name": row.get("Merchant", "Unknown"),
                "amount": float(row.get("Amount", 0)),
                "category": row.get("Category", "Uncategorized"),
            })
    return items


# ── Monarch API ────────────────────────────────────────────────────────────────

async def _fetch_page(mm, start_date: date, end_date: date) -> list[dict]:
    raw = await mm.get_transactions(
        limit=MONARCH_LIMIT,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )
    return [_make_item(t) for t in raw.get("allTransactions", {}).get("results", [])]


async def _fetch_all(mm) -> list[dict]:
    """Fetch complete transaction history from Monarch, paginating year by year."""
    seen: dict[str, dict] = {}
    end_date = date.today()

    while True:
        page = await _fetch_page(mm, start_date=end_date.replace(month=1, day=1), end_date=end_date)
        if not page:
            break
        for item in page:
            if item.get("id"):
                seen[item["id"]] = item
        oldest = min(_parse_date(item["date"]) for item in page)
        print(f"  Fetched {len(page)} transactions back to {oldest} ({len(seen)} total)")
        end_date = oldest - timedelta(days=1)

    return sorted(seen.values(), key=lambda t: t["date"], reverse=True)


# ── Public API ─────────────────────────────────────────────────────────────────

async def get_transactions() -> list[dict]:
    """Return the full transaction list.

    Reads MONARCH_CSV_PATH (or falls back to USE_CSV flag) for CSV mode.
    Otherwise fetches fresh from Monarch on every run — no server-side cache.
    """
    csv_path_str = os.environ.get("MONARCH_CSV_PATH", "")
    csv_path = Path(csv_path_str) if csv_path_str else None

    if os.environ.get("USE_CSV") == "1":
        print("CSV mode — reading uploaded transactions.")
        if not csv_path or not csv_path.exists():
            raise FileNotFoundError("No CSV found. Upload a transactions file first.")
        return load_from_csv(csv_path)

    try:
        mm = await Monarch.get_client()
        print("Fetching transactions from Monarch...")
        return await _fetch_all(mm)
    except ValueError:
        # Auth errors (expired session, missing credentials) are not recoverable via CSV
        raise
    except Exception as e:
        print(f"Monarch API unavailable ({e}), falling back to CSV.")

    if not csv_path or not csv_path.exists():
        raise FileNotFoundError(
            "No CSV fallback found. Fix Monarch credentials or upload a transactions CSV."
        )
    return load_from_csv(csv_path)
