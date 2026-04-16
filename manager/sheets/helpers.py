import calendar
import functools
import time
from datetime import date, datetime

import gspread


def retry_on_quota(fn):
    """Retry the decorated function on gspread 429 quota errors, sleeping 60 s between attempts."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        while True:
            try:
                return fn(*args, **kwargs)
            except gspread.exceptions.APIError as e:
                if e.response.status_code == 429:
                    time.sleep(60)
                else:
                    raise
    return wrapper


def _month_label(m: str, include_year: bool = False) -> str:
    fmt = "%b '%y" if include_year else "%b"
    label = datetime.strptime(m, "%Y-%m").strftime(fmt)
    today = date.today()
    if m == today.strftime("%Y-%m"):
        last_day = calendar.monthrange(today.year, today.month)[1]
        if today.day != last_day:
            label += " (partial)"
    return label
