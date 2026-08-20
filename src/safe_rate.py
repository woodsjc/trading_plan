import logging
import os
from datetime import date
from typing import Any

import requests

from parsing import parse_date


def get_fred_rate(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Fetch risk-free rate from FRED.

    Default series DTB3 is 3-month T-bill, quoted in percent.
    """
    api_key = os.getenv("FRED_API_KEY", cfg.get("fred_api_key", ""))
    series = cfg.get("fred_series", "DTB3")
    default_rate = cfg.get("default_rf_rate")

    if api_key:
        url = "https://api.stlouisfed.org/fred/series/observations"
        params = {
            "series_id": series,
            "api_key": api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
        }

        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            observations = resp.json().get("observations", [])

            for obs in observations:
                value = obs.get("value")
                if value not in (None, "", "."):
                    rate = float(value) / 100.0
                    return {
                        "rate": rate,
                        "source": f"FRED:{series}",
                        "estimated": False,
                    }

        except Exception as exc:
            logging.warning("FRED API failed: %s", exc)

    if cfg.get("allow_default_rf_rate", True) and default_rate is not None:
        asof = parse_date(cfg.get("default_rf_rate_asof"))
        if asof is not None:
            stale_days = (date.today() - asof).days
            logging.warning(
                "Using static default_rf_rate=%.4f (config_default), last reviewed %s "
                "(%s days ago). Stage 8's risk-free hurdle depends on this being current "
                "— set FRED_API_KEY or refresh default_rf_rate_asof.",
                float(default_rate),
                asof.isoformat(),
                stale_days,
            )
        else:
            logging.warning(
                "Using static default_rf_rate=%.4f (config_default) with no "
                "default_rf_rate_asof set — staleness cannot be assessed.",
                float(default_rate),
            )

        return {
            "rate": float(default_rate),
            "source": "config_default",
            "estimated": True,
        }

    return {
        "rate": None,
        "source": "missing",
        "estimated": True,
    }
