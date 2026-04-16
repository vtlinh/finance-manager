import os

import gspread

from ..login import Google
from .spending import write_spending_sheet
from .summary import generate_all_summary_insights, write_summary_sheet


def export(categorized: list[dict], anomalies: dict) -> None:
    years = sorted({g["month"][:4] for g in categorized})

    spreadsheet_id = os.environ["SPREADSHEET_ID"]
    creds = Google.get_credentials()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)

    expected = {f"Spending {y}" for y in years} | {f"Summary {y}" for y in years}

    def get_or_create(title: str) -> gspread.Worksheet:
        try:
            return sh.worksheet(title)
        except gspread.WorksheetNotFound:
            return sh.add_worksheet(title=title, rows=500, cols=30)

    # Generate all year summaries in a single batch LLM call
    years_data = [
        (year,
         [g for g in categorized if g["month"].startswith(year)],
         [g for g in categorized if g["month"].startswith(str(int(year) - 1))])
        for year in years
    ]
    all_insights = generate_all_summary_insights(years_data)

    # Write expected tabs first so there is always at least one tab before any deletions
    for year, year_groups, prev_year_groups in years_data:
        print(f"Writing Spending {year}...")
        write_spending_sheet(get_or_create(f"Spending {year}"), year_groups, anomalies)

        print(f"Writing Summary {year}...")
        write_summary_sheet(get_or_create(f"Summary {year}"), year_groups, prev_year_groups, anomalies, year, all_insights[year])

    # Now safe to clean up stale tabs — expected tabs already exist
    for ws in sh.worksheets():
        if ws.title in expected:
            continue
        if ws.title.startswith("_"):
            continue  # cache tabs — leave untouched
        print(f"Removing old tab: {ws.title}")
        sh.del_worksheet(ws)

    # Reorder tabs chronologically: Spending {year}, Summary {year} for each year
    desired_order = [title for year in years for title in [f"Spending {year}", f"Summary {year}"]]
    worksheets_by_title = {ws.title: ws for ws in sh.worksheets()}
    sh.reorder_worksheets([worksheets_by_title[t] for t in desired_order if t in worksheets_by_title])

    print(f"Done — https://docs.google.com/spreadsheets/d/{spreadsheet_id}")
