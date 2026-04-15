import asyncio
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

# Load credentials from temp config file written by the web server.
# Must run before any other imports that might read os.environ.
_cfg = os.environ.pop("FINANCE_CONFIG", None)
if _cfg:
    try:
        with open(_cfg, encoding="utf-8") as _f:
            for _k, _v in json.load(_f).items():
                if _v:
                    os.environ[_k] = _v
    finally:
        try:
            os.unlink(_cfg)
        except OSError:
            pass

from dotenv import load_dotenv

from .anomaly import detect_monthly_anomalies
from .categorize_transactions import categorize_with_llm, filter_transactions, group_by_month_merchant
from .export_to_sheets import export, print_spreadsheet
from .load_transactions import get_transactions

load_dotenv()


async def main() -> None:
    transactions = await get_transactions()
    print(f"Loaded {len(transactions)} transactions.\n")

    transactions = filter_transactions(transactions)
    groups = group_by_month_merchant(transactions)

    categorized = categorize_with_llm(groups)
    anomalies = detect_monthly_anomalies(categorized)

    # print()
    # print_spreadsheet(categorized, anomalies)
    export(categorized, anomalies)


def main_sync() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
