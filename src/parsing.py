from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


def safe_float(x: Any) -> float | None:
    if x is None or pd.isna(x) or isinstance(x, str) and x.strip() == "":
        return None
    return float(x)


def parse_date(value: Any) -> date | None:
    if value is None or pd.isna(value):
        return None
    elif isinstance(value, datetime):
        return value.date()
    elif isinstance(value, date):
        return value
    elif isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=ZoneInfo("America/New_York")).date()
        except Exception:
            return None

    parsed = pd.to_datetime(str(value))
    if pd.isna(parsed):
        return None
    return parsed.date()
