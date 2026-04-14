import asyncio
from dotenv import load_dotenv
from load_transactions import get_transactions

load_dotenv()


async def main():
    items = await get_transactions()
    print(f"Found {len(items)} transactions\n")
    for t in items:
        print(f"{t['date']}  {t['name']:<40}  ${t['amount']:>10.2f}  {t['category']}")


if __name__ == "__main__":
    asyncio.run(main())
