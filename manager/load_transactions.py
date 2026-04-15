import csv
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from .login import Monarch

_DIR = Path(__file__).parent
TRANSACTIONS_FILE = _DIR / ".monarch_transactions"
CACHE_FILE = _DIR / ".monarch_transactions.json"
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


# ── Store (transactions + metadata) ───────────────────────────────────────────

def _load_store() -> tuple[dict[str, dict], dict]:
    """Returns (transactions_by_id, meta)."""
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        by_id = {t["id"]: t for t in data.get("transactions", []) if t.get("id")}
        return by_id, data.get("meta", {})
    return {}, {}


def _save_store(by_id: dict[str, dict], meta: dict) -> None:
    transactions = sorted(by_id.values(), key=lambda t: t["date"], reverse=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "transactions": transactions}, f, indent=2)


# ── CSV fallback ───────────────────────────────────────────────────────────────

def load_from_csv(
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> list[dict]:
    items = []
    with open(TRANSACTIONS_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            row_date = _parse_date(row.get("Date", ""))
            if start and row_date < start:
                continue
            if end and row_date > end:
                break
            items.append({
                "date": row.get("Date", ""),
                "name": row.get("Merchant", "Unknown"),
                "amount": float(row.get("Amount", 0)),
                "category": row.get("Category", "Uncategorized"),
            })
    return items


# ── API fetching ───────────────────────────────────────────────────────────────

async def _fetch_page(mm, start_date: date, end_date: date) -> list[dict]:
    raw = await mm.get_transactions(limit=MONARCH_LIMIT, start_date=start_date.isoformat(), end_date=end_date.isoformat())
    return [_make_item(t) for t in raw.get("allTransactions", {}).get("results", [])]


async def _fetch_full_history(mm, store: dict[str, dict], meta: dict) -> None:
    """Paginate backward through all history until no results are returned."""
    print("First run — fetching full transaction history...")
    end_date = date.today()
    total = 0

    while True:
        page = await _fetch_page(mm, start_date=end_date.replace(month=1, day=1), end_date=end_date)
        if not page:
            break

        for item in page:
            if item.get("id") and item["id"] not in store:
                store[item["id"]] = item
                total += 1

        oldest = min(_parse_date(item["date"]) for item in page)
        print(f"  Fetched {len(page)} transactions (oldest: {oldest}, total so far: {total})")
        end_date = oldest - timedelta(days=1)  # Continue paginating backward from the oldest date

    meta["full_history_fetched"] = True
    meta["last_fetched_date"] = date.today().isoformat()
    print(f"Full history fetched: {total} transactions.")


async def _fetch_since(mm, last_fetched: date, store: dict[str, dict], meta: dict) -> None:
    """Fetch only transactions added or updated since the last fetch date."""
    print(f"Fetching new transactions since {last_fetched}...")
    page = await _fetch_page(mm, start_date=last_fetched, end_date=date.today())

    added = updated = 0
    for item in page:
        tid = item.get("id")
        if not tid:
            continue
        if tid not in store:
            added += 1
        elif store[tid] != item:
            updated += 1
        store[tid] = item

    meta["last_fetched_date"] = date.today().isoformat()
    print(f"Transactions: {added} new, {updated} updated ({len(store)} total in cache).")


# ── Public API ─────────────────────────────────────────────────────────────────

async def get_transactions() -> list[dict]:
    """Return the full list of transactions.

    On first run, fetches the full history by paginating backward.
    On subsequent runs, fetches only new transactions since the last fetch.
    Falls back to load_from_csv() if the Monarch API is unreachable.
    Raises if neither source is available.
    """
    store, meta = _load_store()

    try:
        mm = await Monarch.get_client()
        if not meta.get("full_history_fetched"):
            await _fetch_full_history(mm, store, meta)
        else:
            last_fetched = _parse_date(meta["last_fetched_date"])
            await _fetch_since(mm, last_fetched, store, meta)
        _save_store(store, meta)
        return sorted(store.values(), key=lambda t: t["date"], reverse=True)
    except Exception as e:
        print(f"Monarch API unavailable ({e}), falling back to CSV.")

    if not TRANSACTIONS_FILE.exists():
        raise FileNotFoundError(
            f"No CSV fallback found at {TRANSACTIONS_FILE}. "
            "Fix Monarch credentials or export transactions to that file."
        )

    return load_from_csv()
