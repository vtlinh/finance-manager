import calendar
from datetime import date, datetime


def _month_label(m: str, include_year: bool = False) -> str:
    fmt = "%b '%y" if include_year else "%b"
    label = datetime.strptime(m, "%Y-%m").strftime(fmt)
    today = date.today()
    if m == today.strftime("%Y-%m"):
        last_day = calendar.monthrange(today.year, today.month)[1]
        if today.day != last_day:
            label += " (partial)"
    return label
