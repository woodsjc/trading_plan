from datetime import date, datetime
from typing import Any

import pandas as pd


def safe_float(x: Any) -> float | None:
    try:
        if x is None:
            return None
        try:
            if pd.isna(x):
                return None
        except Exception:
            pass
        if isinstance(x, str) and x.strip() == "":
            return None
        return float(x)
    except TypeError, ValueError:
        return None


def parse_date(value: Any) -> date | None:
    try:
        if value is None:
            return None

        try:
            if pd.isna(value):
                return None
        except Exception:
            pass

        if isinstance(value, datetime):
            return value.date()

        if isinstance(value, date):
            return value

        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:
                ts = ts / 1000.0
            try:
                return datetime.fromtimestamp(ts).date()
            except Exception:
                return None

        parsed = pd.to_datetime(str(value))
        if pd.isna(parsed):
            return None
        return parsed.date()

    except Exception:
        return None
