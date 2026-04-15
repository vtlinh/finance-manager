import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")

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
