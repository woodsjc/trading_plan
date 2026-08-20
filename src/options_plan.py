import importlib.util
import logging
import math
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from database import load_options_contracts, load_underlying_metrics
from parsing import parse_date, safe_float


def call_with_backoff(
    fn,
    *args,
    max_retries: int = 3,
    base_delay: float = 1.0,
    label: str = "",
    **kwargs,
):
    """
    Call fn(*args, **kwargs) with exponential backoff on exception.

    yfinance is an unofficial scraper with no documented rate limits;
    a batch of ~100 symbols x multiple expirations can trigger throttling
    or transient failures. This wrapper retries with increasing delay
    instead of failing (and silently downgrading the symbol to
    DATA INCOMPLETE) on the first hiccup.
    """
    attempt = 0
    last_exc: Optional[Exception] = None

    while attempt <= max_retries:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, this wraps 3rd-party calls
            last_exc = exc
            attempt += 1
            if attempt > max_retries:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logging.warning(
                "%s failed (attempt %s/%s): %s — retrying in %.1fs",
                label or getattr(fn, "__name__", "call"),
                attempt,
                max_retries,
                exc,
                delay,
            )
            time.sleep(delay)

    logging.error(
        "%s failed after %s attempts: %s",
        label or getattr(fn, "__name__", "call"),
        max_retries,
        last_exc,
    )
    raise last_exc if last_exc else RuntimeError("call_with_backoff: unknown failure")


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(
    spot: float,
    strike: float,
    t_years: float,
    rf: float,
    iv: float,
    option_type: str,
) -> Optional[float]:
    """
    Black-Scholes delta approximation.

    Yahoo Finance does not provide Greeks by default.
    This is a model-based estimate from quoted IV.
    """
    try:
        if spot <= 0 or strike <= 0 or t_years <= 0 or iv <= 0:
            return None

        d1 = (math.log(spot / strike) + (rf + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))

        if option_type.upper().startswith("C"):
            return norm_cdf(d1)

        return norm_cdf(d1) - 1.0

    except Exception:
        return None


# ----------------------------------------------------------------------------
# Historical price / realized volatility
# ----------------------------------------------------------------------------


def fetch_price_history(
    symbols: List[str],
    period: str = "1y",
    max_retries: int = 3,
    retry_backoff_seconds: float = 1.0,
) -> Dict[str, Dict[str, Any]]:
    """
    Batch-download price history for all symbols.

    This is the part that efficiently handles ~100 symbols at once.
    """
    out: Dict[str, Dict[str, Any]] = {
        sym: {
            "last_price": None,
            "rv20": None,
            "rv30": None,
            "rv30_series": pd.Series(dtype=float),
        }
        for sym in symbols
    }

    if not symbols:
        return out

    logging.info("Downloading batch price history for %s symbols", len(symbols))

    try:
        data = call_with_backoff(
            yf.download,
            tickers=symbols,
            period=period,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
            max_retries=max_retries,
            base_delay=retry_backoff_seconds,
            label="yf.download(batch price history)",
        )
    except Exception as exc:
        logging.error("yf.download failed after retries: %s", exc)
        return out

    if data is None or data.empty:
        logging.warning("Price history download returned empty data")
        return out

    for sym in symbols:
        try:
            close: Optional[pd.Series] = None

            if isinstance(data.columns, pd.MultiIndex):
                level0 = data.columns.get_level_values(0)

                if sym in level0:
                    close = data[sym]["Close"]
                elif len(symbols) == 1:
                    if "Close" in data.columns:
                        close = data["Close"]
                    else:
                        close = data.iloc[:, 0]
            else:
                if "Close" in data.columns:
                    close = data["Close"]
                elif len(data.columns) > 0:
                    close = data[data.columns[0]]

            if close is None:
                continue

            close = close.dropna()
            if close.empty:
                continue

            log_ret = np.log(close / close.shift(1)).dropna()

            rv20 = None
            rv30 = None
            rv30_series = pd.Series(dtype=float)

            if len(log_ret) >= 20:
                rv20_series = log_ret.rolling(20).std() * math.sqrt(252)
                rv20 = safe_float(rv20_series.iloc[-1])

            if len(log_ret) >= 30:
                rv30_series = (log_ret.rolling(30).std() * math.sqrt(252)).dropna()
                if not rv30_series.empty:
                    rv30 = safe_float(rv30_series.iloc[-1])

            out[sym] = {
                "last_price": safe_float(close.iloc[-1]),
                "rv20": rv20,
                "rv30": rv30,
                "rv30_series": rv30_series,
            }

        except Exception as exc:
            logging.warning("Price history failed for %s: %s", sym, exc)

    return out


# ----------------------------------------------------------------------------
# Fundamentals
# ----------------------------------------------------------------------------


def get_earnings_date(tk: yf.Ticker, info: Dict[str, Any]) -> Optional[date]:
    candidates: List[date] = []

    for key in [
        "earningsTimestamp",
        "earningsTimestampStart",
        "earningsTimestampEnd",
        "earningsDate",
    ]:
        parsed = parse_date(info.get(key))
        if parsed:
            candidates.append(parsed)

    try:
        cal = tk.calendar

        if isinstance(cal, dict):
            ed = cal.get("Earnings Date") or cal.get("Earnings Date Start")
            if isinstance(ed, list):
                for item in ed:
                    parsed = parse_date(item)
                    if parsed:
                        candidates.append(parsed)
            else:
                parsed = parse_date(ed)
                if parsed:
                    candidates.append(parsed)

        elif isinstance(cal, pd.DataFrame):
            if "Earnings Date" in cal.index:
                row = cal.loc["Earnings Date"]
                if isinstance(row, (list, tuple, pd.Series)):
                    for item in row:
                        parsed = parse_date(item)
                        if parsed:
                            candidates.append(parsed)
                else:
                    parsed = parse_date(row)
                    if parsed:
                        candidates.append(parsed)

    except Exception:
        pass

    try:
        earnings_dates = tk.earnings_dates
        if earnings_dates is not None and not earnings_dates.empty:
            for idx in earnings_dates.index:
                parsed = parse_date(idx)
                if parsed and parsed >= date.today() - timedelta(days=1):
                    candidates.append(parsed)
    except Exception:
        pass

    future = [d for d in candidates if d and d >= date.today() - timedelta(days=1)]
    return min(future) if future else None


def fetch_fundamentals(
    symbol: str,
    max_retries: int = 3,
    retry_backoff_seconds: float = 1.0,
) -> Dict[str, Any]:
    tk = yf.Ticker(symbol)

    info: Dict[str, Any] = {}
    try:
        info = call_with_backoff(
            lambda: tk.info or {},
            max_retries=max_retries,
            base_delay=retry_backoff_seconds,
            label=f"tk.info({symbol})",
        )
    except Exception as exc:
        logging.warning("Info failed for %s after retries: %s", symbol, exc)

    price = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose")

    avg_volume = info.get("averageVolume") or info.get("averageDailyVolume10Day") or info.get("volume")

    market_cap = info.get("marketCap")
    beta = info.get("beta")

    dividend_amount = info.get("dividendRate") or info.get("trailingAnnualDividendRate")

    ex_div_date = parse_date(info.get("exDividendDate"))
    earnings_date = get_earnings_date(tk, info)

    return {
        "price": safe_float(price),
        "average_volume": safe_float(avg_volume),
        "market_cap": safe_float(market_cap),
        "beta": safe_float(beta),
        "dividend_amount": safe_float(dividend_amount),
        "ex_div_date": ex_div_date,
        "earnings_date": earnings_date,
    }


# ----------------------------------------------------------------------------
# Options chain
# ----------------------------------------------------------------------------


def fetch_options_chain(
    symbol: str,
    spot: Optional[float],
    rf_rate: Optional[float],
    cfg: Dict[str, Any],
) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []

    if spot is None or spot <= 0:
        return pd.DataFrame(rows)

    max_retries = int(cfg.get("max_retries", 3))
    retry_backoff_seconds = float(cfg.get("retry_backoff_seconds", 1.0))
    request_delay_seconds = float(cfg.get("request_delay_seconds", 0.05))

    tk = yf.Ticker(symbol)

    try:
        expirations = call_with_backoff(
            lambda: tk.options,
            max_retries=max_retries,
            base_delay=retry_backoff_seconds,
            label=f"tk.options({symbol})",
        )
    except Exception as exc:
        logging.warning("Options expirations failed for %s after retries: %s", symbol, exc)
        return pd.DataFrame(rows)

    if not expirations:
        return pd.DataFrame(rows)

    today = date.today()
    min_dte_fetch = int(cfg.get("min_dte_fetch", 30))
    max_dte_fetch = int(cfg.get("max_dte_fetch", 40))
    rf_for_delta = rf_rate if rf_rate is not None else 0.0
    now_iso = datetime.now().isoformat()
    iv_rescale_count = 0

    for exp in expirations:
        try:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
        except Exception:
            continue

        dte = (exp_date - today).days
        if dte < min_dte_fetch or dte > max_dte_fetch:
            continue

        try:
            chain = call_with_backoff(
                tk.option_chain,
                exp,
                max_retries=max_retries,
                base_delay=retry_backoff_seconds,
                label=f"tk.option_chain({symbol}, {exp})",
            )
        except Exception as exc:
            logging.warning("Option chain failed for %s %s after retries: %s", symbol, exp, exc)
            continue

        datasets = [
            ("C", chain.calls),
            ("P", chain.puts),
        ]

        for option_type, df in datasets:
            if df is None or df.empty:
                continue

            for _, r in df.iterrows():
                strike = safe_float(r.get("strike"))
                bid = safe_float(r.get("bid"))
                ask = safe_float(r.get("ask"))

                if strike is None or bid is None or ask is None:
                    continue

                if bid <= 0 or ask <= 0 or ask < bid:
                    continue

                midpoint = (bid + ask) / 2.0
                spread = ask - bid
                spread_pct = spread / midpoint if midpoint > 0 else None

                volume = safe_float(r.get("volume")) or 0.0
                open_interest = safe_float(r.get("openInterest")) or 0.0

                iv = safe_float(r.get("impliedVolatility"))
                if iv is not None and iv > 3.0:
                    # Yahoo should usually report IV as decimal, e.g. 0.35.
                    # If a feed reports 35.0, normalize to 0.35.
                    #
                    # CAUTION: this heuristic can misfire on genuinely
                    # high-IV contracts (deep OTM / small-cap / event-driven
                    # names can legitimately show IV > 300%). Log every
                    # rescale so it can be audited rather than applied
                    # silently.
                    logging.info(
                        "IV rescale applied: %s %s %s strike=%s raw_iv=%.3f -> %.4f",
                        symbol,
                        exp_date.isoformat(),
                        option_type,
                        strike,
                        iv,
                        iv / 100.0,
                    )
                    iv = iv / 100.0
                    iv_rescale_count += 1

                if iv is not None and iv <= 0:
                    iv = None

                t_years = max(dte, 1) / 365.0
                delta = bs_delta(spot, strike, t_years, rf_for_delta, iv, option_type) if iv else None

                if option_type == "C":
                    intrinsic = max(0.0, spot - strike)
                else:
                    intrinsic = max(0.0, strike - spot)

                extrinsic = midpoint - intrinsic

                contract_symbol = str(
                    r.get("contractSymbol") or f"{symbol}_{exp_date.isoformat()}_{strike}_{option_type}"
                )

                rows.append(
                    {
                        "contract_id": contract_symbol,
                        "symbol": symbol,
                        "expiration": exp_date.isoformat(),
                        "dte": dte,
                        "strike": strike,
                        "type": option_type,
                        "bid": bid,
                        "ask": ask,
                        "midpoint": midpoint,
                        "spread": spread,
                        "spread_pct": spread_pct,
                        "volume": volume,
                        "open_interest": open_interest,
                        "iv": iv,
                        "delta": delta,
                        "extrinsic_value": extrinsic,
                        "last_updated": now_iso,
                    }
                )

        # Throttle between expirations to reduce rate-limit risk (configurable).
        time.sleep(request_delay_seconds)

    if iv_rescale_count:
        logging.info(
            "%s: IV /100 rescale heuristic applied to %s contract(s) — review if unexpected",
            symbol,
            iv_rescale_count,
        )

    return pd.DataFrame(rows)


def compute_iv30(options_df: pd.DataFrame, spot: Optional[float]) -> Optional[float]:
    """
    Estimate IV30 from nearest 30-day expiration ATM options.

    This is not a full term-structure model. It is a practical approximation.
    """
    if options_df.empty or spot is None or spot <= 0:
        return None

    df = options_df.copy()

    df = df[df["dte"].between(20, 45) & df["iv"].notna() & (df["iv"] > 0)]

    if df.empty:
        return None

    df["distance_to_spot"] = (df["strike"] - spot).abs()

    try:
        idx = df.groupby("expiration")["distance_to_spot"].idxmin()
        atm = df.loc[idx]
        iv30 = safe_float(atm["iv"].median())
        return iv30 if iv30 and iv30 > 0 else None
    except Exception:
        return None


def load_iv_rank_overrides(path: str) -> Dict[str, Optional[float]]:
    if not path:
        return {}

    p = Path(path)
    if not p.exists():
        return {}

    try:
        df = pd.read_csv(p)
        if "symbol" not in df.columns or "iv_rank" not in df.columns:
            return {}

        out: Dict[str, Optional[float]] = {}
        for _, row in df.iterrows():
            sym = str(row["symbol"]).strip().upper()
            out[sym] = safe_float(row["iv_rank"])
        return out

    except Exception as exc:
        logging.warning("Could not load IV Rank overrides: %s", exc)
        return {}


def get_iv_rank(
    symbol: str,
    iv30: Optional[float],
    rv30_series: Optional[pd.Series],
    overrides: Dict[str, Optional[float]],
    cfg: Dict[str, Any],
    iv30_history: Optional[pd.Series] = None,
) -> Dict[str, Any]:
    """
    IV Rank hierarchy:

    1. Manual override from broker / vendor.
    2. Real historical-IV-based rank, once enough locally-accrued daily
       IV30 observations exist (iv_history table, see min_iv_history_days).
       This is what "IV Rank" is supposed to mean: today's IV vs its own
       history — not a proxy against realized volatility.
    3. Fallback planning-only estimate using historical realized volatility
       (IV vs RV, not IV vs IV history). This is a materially weaker,
       different signal and is always marked estimated=True.
    4. Missing.
    """
    if symbol in overrides and overrides[symbol] is not None:
        return {
            "iv_rank": overrides[symbol],
            "source": "manual_override",
            "estimated": False,
        }

    min_iv_history_days = int(cfg.get("min_iv_history_days", 100))

    if iv30 is not None and iv30_history is not None:
        hist = iv30_history.dropna()
        if len(hist) >= min_iv_history_days:
            rank = float((hist < iv30).mean() * 100.0)
            return {
                "iv_rank": rank,
                "source": "historical_iv_rank",
                "estimated": False,
            }

    if (
        cfg.get("allow_estimated_iv_rank", False)
        and iv30 is not None
        and rv30_series is not None
        and len(rv30_series) > 30
    ):
        series = rv30_series.dropna()
        if not series.empty:
            rank = float((series < iv30).mean() * 100.0)
            return {
                "iv_rank": rank,
                "source": "estimated_rv_proxy",
                "estimated": True,
            }

    return {
        "iv_rank": None,
        "source": "missing",
        "estimated": True,
    }


# ----------------------------------------------------------------------------
# Collection orchestration
# ----------------------------------------------------------------------------


def empty_underlying(symbol: str, notes: str, rf_meta: Dict[str, Any]) -> Dict[str, Any]:
    now_iso = datetime.now().isoformat()

    return {
        "symbol": symbol,
        "last_updated": now_iso,
        "price": None,
        "average_volume": None,
        "market_cap": None,
        "beta": None,
        "earnings_date": None,
        "ex_div_date": None,
        "div_amount": None,
        "iv30": None,
        "iv_rank": None,
        "iv_rank_source": "missing",
        "iv_rank_estimated": 1,
        "rv20d": None,
        "rv30d": None,
        "vrp_pp": None,
        "vrp_ratio": None,
        "rf_rate": rf_meta.get("rate"),
        "rf_source": rf_meta.get("source"),
        "rf_estimated": 1 if rf_meta.get("estimated") else 0,
        "notes": notes,
    }


def process_symbol(
    symbol: str,
    cfg: Dict[str, Any],
    history: Dict[str, Dict[str, Any]],
    overrides: Dict[str, Optional[float]],
    rf_meta: Dict[str, Any],
    iv30_history_map: Optional[Dict[str, pd.Series]] = None,
) -> Dict[str, Any]:
    try:
        max_retries = int(cfg.get("max_retries", 3))
        retry_backoff_seconds = float(cfg.get("retry_backoff_seconds", 1.0))

        fund = fetch_fundamentals(
            symbol,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        hist = history.get(symbol, {})

        spot = safe_float(fund.get("price"))
        if spot is None:
            spot = safe_float(hist.get("last_price"))

        if spot is None:
            return {
                "underlying": empty_underlying(symbol, "No price available", rf_meta),
                "options": pd.DataFrame(),
            }

        options_df = fetch_options_chain(symbol, spot, rf_meta.get("rate"), cfg)

        iv30 = compute_iv30(options_df, spot) if not options_df.empty else None

        iv_rank_data = get_iv_rank(
            symbol=symbol,
            iv30=iv30,
            rv30_series=hist.get("rv30_series"),
            overrides=overrides,
            cfg=cfg,
            iv30_history=(iv30_history_map or {}).get(symbol),
        )

        rv20 = hist.get("rv20")
        rv30 = hist.get("rv30")

        vrp_pp = None
        vrp_ratio = None

        if iv30 is not None and rv30 is not None:
            vrp_pp = (iv30 - rv30) * 100.0
            if rv30 > 0:
                vrp_ratio = iv30 / rv30

        underlying = {
            "symbol": symbol,
            "last_updated": datetime.now().isoformat(),
            "price": spot,
            "average_volume": fund.get("average_volume"),
            "market_cap": fund.get("market_cap"),
            "beta": fund.get("beta"),
            "earnings_date": (fund.get("earnings_date").isoformat() if fund.get("earnings_date") else None),
            "ex_div_date": (fund.get("ex_div_date").isoformat() if fund.get("ex_div_date") else None),
            "div_amount": fund.get("dividend_amount"),
            "iv30": iv30,
            "iv_rank": iv_rank_data["iv_rank"],
            "iv_rank_source": iv_rank_data["source"],
            "iv_rank_estimated": 1 if iv_rank_data["estimated"] else 0,
            "rv20d": rv20,
            "rv30d": rv30,
            "vrp_pp": vrp_pp,
            "vrp_ratio": vrp_ratio,
            "rf_rate": rf_meta.get("rate"),
            "rf_source": rf_meta.get("source"),
            "rf_estimated": 1 if rf_meta.get("estimated") else 0,
            "notes": None,
        }

        return {
            "underlying": underlying,
            "options": options_df,
        }

    except Exception as exc:
        logging.exception("process_symbol failed for %s", symbol)
        return {
            "underlying": empty_underlying(symbol, f"process_symbol exception: {exc}", rf_meta),
            "options": pd.DataFrame(),
        }


# ----------------------------------------------------------------------------
# Evaluation / v3.3 gates
# ----------------------------------------------------------------------------


def base_result(symbol: str) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "decision_label": "⚪ DATA INCOMPLETE — DO NOT AUTHORIZE",
        "strategy": None,
        "contract_id": None,
        "strike": None,
        "expiration": None,
        "dte": None,
        "delta": None,
        "iv": None,
        "iv_rank": None,
        "vrp_ratio": None,
        "open_interest": None,
        "volume": None,
        "spread_pct": None,
        "net_premium": None,
        "premium_cost_ratio": None,
        "score": None,
        "beta": None,
        "price": None,
        "reasons": [],
    }


def evaluate_symbol(
    symbol: str,
    cfg: Dict[str, Any],
    conn: sqlite3.Connection,
) -> Dict[str, Any]:
    result = base_result(symbol)
    warnings: List[str] = []
    planning_only = False

    def reject(msg: str) -> Dict[str, Any]:
        result["decision_label"] = "🔴 REJECT"
        result["reasons"].append(msg)
        return result

    def incomplete(msg: str) -> Dict[str, Any]:
        result["decision_label"] = "⚪ DATA INCOMPLETE — DO NOT AUTHORIZE"
        result["reasons"].append(msg)
        return result

    screen = cfg.get("screen", {})
    portfolio = cfg.get("portfolio", {})

    strict_evidence = bool(cfg.get("strict_evidence", False))
    require_earnings_date = bool(cfg.get("require_earnings_date", True))

    # ------------------------------------------------------------------
    # Load data
    # ------------------------------------------------------------------

    underlying_df = load_underlying_metrics(conn, symbol)

    if underlying_df.empty:
        return incomplete("No collected data for symbol")

    u = underlying_df.iloc[0].to_dict()

    # ------------------------------------------------------------------
    # Stage 0: evidence completeness
    # ------------------------------------------------------------------

    required_fields = [
        "price",
        "average_volume",
        "market_cap",
        "beta",
        "iv30",
        "iv_rank",
        "rv20d",
        "rv30d",
        "rf_rate",
    ]

    missing = [f for f in required_fields if safe_float(u.get(f)) is None]

    if require_earnings_date and parse_date(u.get("earnings_date")) is None:
        missing.append("earnings_date")

    if missing:
        msg = f"Stage 0 missing/unverified: {', '.join(missing)}"
        if strict_evidence:
            return incomplete(msg)

        planning_only = True
        warnings.append(msg)

    if bool(u.get("iv_rank_estimated")):
        msg = "IV Rank is estimated or not independently verified"
        if strict_evidence:
            return incomplete(msg)
        planning_only = True
        warnings.append(msg)

    if bool(u.get("rf_estimated")):
        planning_only = True
        warnings.append("Risk-free rate is default/estimated")

    max_data_age_hours = safe_float(cfg.get("max_data_age_hours", 24)) or 24.0
    last_updated_raw = u.get("last_updated")

    try:
        last_updated_dt = datetime.fromisoformat(str(last_updated_raw))
        age_hours = (datetime.now() - last_updated_dt).total_seconds() / 3600.0

        if age_hours > max_data_age_hours:
            msg = f"Stage 0: data is {age_hours:.1f}h old, exceeds " f"max_data_age_hours={max_data_age_hours}"
            if strict_evidence:
                return incomplete(msg)

            planning_only = True
            warnings.append(msg)
    except Exception:
        msg = f"Stage 0: last_updated timestamp missing or unparseable ({last_updated_raw!r})"
        if strict_evidence:
            return incomplete(msg)
        planning_only = True
        warnings.append(msg)

    # ------------------------------------------------------------------
    # Stage 1: underlying screen
    # ------------------------------------------------------------------

    market_cap = safe_float(u.get("market_cap"))
    avg_volume = safe_float(u.get("average_volume"))
    beta = safe_float(u.get("beta"))

    min_market_cap = safe_float(screen.get("min_market_cap", 0)) or 0.0
    min_avg_volume = safe_float(screen.get("min_avg_volume", 0)) or 0.0

    if market_cap is None or market_cap < min_market_cap:
        return reject("Stage 1: market cap below minimum or unavailable")

    if avg_volume is None or avg_volume < min_avg_volume:
        return reject("Stage 1: average volume below minimum or unavailable")

    if beta is not None and beta > 1.5:
        warnings.append("High beta: use smaller size and stricter stress assumptions")

    # ------------------------------------------------------------------
    # Stage 2: volatility screen
    # ------------------------------------------------------------------

    iv_rank = safe_float(u.get("iv_rank"))
    iv30 = safe_float(u.get("iv30"))
    rv20 = safe_float(u.get("rv20d"))
    rv30 = safe_float(u.get("rv30d"))
    vrp_ratio = safe_float(u.get("vrp_ratio"))

    iv_rank_min = safe_float(screen.get("iv_rank_min", 30.0)) or 30.0
    vrp_ratio_min = safe_float(screen.get("vrp_ratio_min", 1.05)) or 1.05

    if iv_rank is None or iv_rank < iv_rank_min:
        return reject(f"Stage 2: IV Rank below {iv_rank_min}")

    if vrp_ratio is None or vrp_ratio < vrp_ratio_min:
        return reject(f"Stage 2: VRP ratio below {vrp_ratio_min}")

    if iv30 is None or rv30 is None or iv30 <= rv30:
        return reject("Stage 2: primary VRP is not positive")

    if rv20 is not None and iv30 <= rv20:
        warnings.append("Stage 2: IV30 <= RV20D, multi-horizon VRP confirmation is weak")

    result["iv_rank"] = iv_rank
    result["vrp_ratio"] = vrp_ratio
    result["beta"] = beta
    result["price"] = safe_float(u.get("price"))

    # ------------------------------------------------------------------
    # Stage 3: catalyst / earnings screen
    # ------------------------------------------------------------------

    earnings_date = parse_date(u.get("earnings_date"))
    dte_max = safe_float(screen.get("dte_max", 40)) or 40.0
    earnings_buffer = safe_float(screen.get("earnings_buffer_days", 7)) or 7.0

    if earnings_date is not None:
        days_to_earnings = (earnings_date - date.today()).days

        if 0 <= days_to_earnings <= dte_max + earnings_buffer:
            return reject(f"Stage 3: earnings in {days_to_earnings} days, inside standard option life")
    else:
        warnings.append("Stage 3: earnings date not verified")

    # ------------------------------------------------------------------
    # Stage 4/5: contract liquidity gates
    # ------------------------------------------------------------------

    contracts_df = load_options_contracts(conn, symbol)

    if contracts_df.empty:
        return reject("Stage 4/5: no options contracts collected")

    numeric_cols = [
        "dte",
        "strike",
        "bid",
        "ask",
        "midpoint",
        "spread",
        "spread_pct",
        "volume",
        "open_interest",
        "iv",
        "delta",
        "extrinsic_value",
    ]

    for col in numeric_cols:
        if col in contracts_df.columns:
            contracts_df[col] = pd.to_numeric(contracts_df[col], errors="coerce")

    contracts_df["abs_delta"] = contracts_df["delta"].abs()

    dte_min = safe_float(screen.get("dte_min", 30)) or 30.0
    dte_max = safe_float(screen.get("dte_max", 40)) or 40.0
    delta_min = safe_float(screen.get("delta_min", 0.15)) or 0.15
    delta_max = safe_float(screen.get("delta_max", 0.30)) or 0.30
    min_open_interest = safe_float(screen.get("min_open_interest", 250)) or 250.0
    min_volume = safe_float(screen.get("min_volume", 10)) or 10.0
    max_spread_pct = safe_float(screen.get("max_spread_pct", 0.10)) or 0.10
    min_premium_cost_ratio = safe_float(screen.get("min_premium_cost_ratio", 5.0)) or 5.0

    valid = contracts_df[
        contracts_df["dte"].between(dte_min, dte_max)
        & (contracts_df["open_interest"].fillna(0) >= min_open_interest)
        & (contracts_df["volume"].fillna(0) >= min_volume)
        & contracts_df["spread_pct"].notna()
        & (contracts_df["spread_pct"] <= max_spread_pct)
        & contracts_df["abs_delta"].between(delta_min, delta_max)
    ].copy()

    if valid.empty:
        return reject("Stage 4/5: no contract satisfies DTE/delta/OI/volume/spread gates")

    # ------------------------------------------------------------------
    # Contract-level economic gates
    # ------------------------------------------------------------------

    holdings = portfolio.get("holdings", {}) or {}
    shares_held = int(holdings.get(symbol, 0))
    allow_buy_write = bool(cfg.get("allow_buy_write", True))
    enforce_sizing = bool(portfolio.get("enforce_sizing", False))

    portfolio_value = safe_float(portfolio.get("value"))
    max_position_pct = safe_float(portfolio.get("max_position_pct"))
    max_position_capital = None

    if portfolio_value is not None and max_position_pct is not None:
        max_position_capital = portfolio_value * max_position_pct / 100.0

    candidates: List[Dict[str, Any]] = []

    for _, c in valid.iterrows():
        candidate_warnings: List[str] = []

        option_type = str(c.get("type", "")).upper().strip()
        strike = safe_float(c.get("strike"))
        dte = safe_float(c.get("dte"))
        midpoint = safe_float(c.get("midpoint"))
        spread = safe_float(c.get("spread"))

        if strike is None or dte is None or midpoint is None or spread is None:
            continue

        # Stage 6: execution-cost / premium sufficiency
        # Use at least $0.01 to avoid division by zero.
        estimated_cost = max(spread, 0.01)
        net_premium = midpoint - estimated_cost

        if net_premium <= 0:
            continue

        premium_cost_ratio = net_premium / estimated_cost

        if premium_cost_ratio < min_premium_cost_ratio:
            continue

        strategy = None

        # --------------------------------------------------------------
        # Covered call / buy-write
        # --------------------------------------------------------------

        if option_type == "C":
            if shares_held >= 100:
                strategy = "Covered Call"
            elif allow_buy_write:
                strategy = "Buy-Write Covered Call"
            else:
                continue

            # Stage 9: dividend / early assignment risk
            ex_div_date = parse_date(u.get("ex_div_date"))
            expiration_date = parse_date(c.get("expiration"))
            dividend_amount = safe_float(u.get("div_amount")) or 0.0

            if ex_div_date and expiration_date and ex_div_date <= expiration_date and dividend_amount > 0:
                extrinsic = safe_float(c.get("extrinsic_value"))

                if extrinsic is None:
                    spot = safe_float(u.get("price"))
                    if spot is not None:
                        intrinsic = max(0.0, spot - strike)
                        extrinsic = midpoint - intrinsic

                if extrinsic is not None and extrinsic < dividend_amount:
                    continue

                candidate_warnings.append("Contract crosses ex-dividend date")

            if strategy == "Buy-Write Covered Call" and enforce_sizing and max_position_capital is not None:
                spot = safe_float(u.get("price"))
                if spot is not None:
                    stock_cost = spot * 100.0
                    if stock_cost > max_position_capital:
                        candidate_warnings.append("Buy-write stock exposure exceeds configured position cap")

        # --------------------------------------------------------------
        # Cash-secured put
        # --------------------------------------------------------------

        elif option_type == "P":
            strategy = "Cash-Secured Put"

            rf_rate = safe_float(u.get("rf_rate"))
            if rf_rate is None:
                continue

            capital_reserved = strike * 100.0
            rf_return = capital_reserved * rf_rate * (dte / 365.0)
            net_premium_contract = net_premium * 100.0

            # Stage 7: risk-free hurdle
            if net_premium_contract < rf_return:
                continue

            if enforce_sizing and max_position_capital is not None:
                if capital_reserved > max_position_capital:
                    continue

        else:
            continue

        candidates.append(
            {
                "strategy": strategy,
                "contract_id": c.get("contract_id"),
                "strike": strike,
                "expiration": c.get("expiration"),
                "dte": int(dte),
                "delta": safe_float(c.get("delta")),
                "iv": safe_float(c.get("iv")),
                "open_interest": safe_float(c.get("open_interest")),
                "volume": safe_float(c.get("volume")),
                "bid": safe_float(c.get("bid")),
                "ask": safe_float(c.get("ask")),
                "midpoint": midpoint,
                "spread": spread,
                "spread_pct": safe_float(c.get("spread_pct")),
                "net_premium": net_premium,
                "premium_cost_ratio": premium_cost_ratio,
                "extrinsic_value": safe_float(c.get("extrinsic_value")),
                "candidate_warnings": candidate_warnings,
            }
        )

    if not candidates:
        return reject("Stage 6/7/9: no contract passed premium, risk hurdle, dividend, or sizing checks")

    # Choose the strongest contract by execution-cost-adjusted premium.
    best = max(
        candidates,
        key=lambda x: (
            x["premium_cost_ratio"],
            x["net_premium"],
        ),
    )

    # ------------------------------------------------------------------
    # Simplified score
    # ------------------------------------------------------------------

    score = 0.0

    if market_cap is not None and market_cap >= 10_000_000_000:
        score += 15.0
    else:
        score += 10.0

    if iv_rank is not None:
        score += min(max(iv_rank, 0.0), 100.0) / 100.0 * 10.0

    if vrp_ratio is not None:
        score += min(max((vrp_ratio - 1.0) / 0.20, 0.0), 1.0) * 20.0

    spread_pct = safe_float(best.get("spread_pct"))
    open_interest = safe_float(best.get("open_interest"))

    if spread_pct is not None and spread_pct <= 0.05 and open_interest is not None and open_interest >= 1000:
        score += 15.0
    else:
        score += 10.0

    if earnings_date is None:
        score += 5.0
    else:
        days_to_earnings = (earnings_date - date.today()).days
        if days_to_earnings > 90:
            score += 10.0
        elif days_to_earnings > dte_max + earnings_buffer:
            score += 7.0
        else:
            score += 2.0

    if best["premium_cost_ratio"] >= 10.0:
        score += 10.0
    else:
        score += 5.0

    score += 10.0  # Basic portfolio placeholder
    score += 5.0 if not warnings else 0.0

    # ------------------------------------------------------------------
    # Final decision label
    # ------------------------------------------------------------------

    result.update({k: v for k, v in best.items() if k != "candidate_warnings"})

    result["score"] = round(score, 1)

    result["reasons"].extend(warnings)
    result["reasons"].extend(best.get("candidate_warnings", []))

    if planning_only:
        result["decision_label"] = "🟡 WAIT"
        result["reasons"].append("Planning only: evidence is estimated/incomplete under v3.3 strict rules")
    else:
        result["decision_label"] = "🟢 AUTHORIZED"

    return result


# ----------------------------------------------------------------------------
# Portfolio-level stages (11-14) — scope gap noted in plan.md
# ----------------------------------------------------------------------------
#
# This pipeline cannot fetch VIX, realized portfolio drawdown, or a true
# beta-weighted delta history for free. Those numbers are supplied
# manually in config.yaml (portfolio.current_vix, current_drawdown_pct,
# etc). What follows is therefore a *reporting/warning* pass over the
# day's AUTHORIZED candidates — an illustrative one-contract-each
# beta-weighted delta exposure and stress result, plus the regime circuit
# breaker checks from Stage 14 — not a fully automated hard gate. It
# exists so `AUTHORIZED` symbols are never presented without at least a
# portfolio-level sanity check, per Stage 32 of the rulebook.


def evaluate_portfolio_stress(results: List[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    portfolio = cfg.get("portfolio", {}) or {}

    benchmark = portfolio.get("benchmark", "SPY")
    benchmark_beta = safe_float(portfolio.get("benchmark_beta", 1.0)) or 1.0
    current_vix = safe_float(portfolio.get("current_vix"))
    current_drawdown_pct = safe_float(portfolio.get("current_drawdown_pct", 0.0)) or 0.0
    hedge_status = str(portfolio.get("hedge_status", "unhedged"))
    stress_shocks_pct = portfolio.get("stress_shocks_pct", [-20, -30]) or [-20, -30]
    regime_review_drawdown = safe_float(portfolio.get("regime_review_drawdown_pct", 10.0)) or 10.0
    regime_severe_drawdown = safe_float(portfolio.get("regime_severe_drawdown_pct", 15.0)) or 15.0
    regime_vix_threshold = safe_float(portfolio.get("regime_vix_threshold", 30.0)) or 30.0
    portfolio_value = safe_float(portfolio.get("value"))

    authorized = [r for r in results if r.get("decision_label") == "🟢 AUTHORIZED"]

    # Illustrative beta-weighted delta exposure: one contract (100 shares
    # notional) per AUTHORIZED short-premium candidate. Short calls/puts
    # both create downside-equivalent delta exposure roughly proportional
    # to |delta| * 100 shares, weighted by the underlying's beta relative
    # to the benchmark. This is a rough aggregate, not a live Greeks feed.
    total_beta_weighted_delta_shares = 0.0
    exposures: List[Dict[str, Any]] = []

    for r in authorized:
        delta = safe_float(r.get("delta"))
        beta = safe_float(r.get("beta"))
        price = safe_float(r.get("price"))

        if delta is None or beta is None or price is None:
            continue

        shares_equivalent = abs(delta) * 100.0
        beta_weighted = shares_equivalent * (beta / benchmark_beta)
        notional = shares_equivalent * price

        total_beta_weighted_delta_shares += beta_weighted
        exposures.append(
            {
                "symbol": r.get("symbol"),
                "strategy": r.get("strategy"),
                "beta": beta,
                "delta": delta,
                "notional": notional,
                "beta_weighted_delta_shares": beta_weighted,
            }
        )

    stress_results = {}
    for shock_pct in stress_shocks_pct:
        shock_frac = safe_float(shock_pct) or 0.0
        # Rough linear approximation: portfolio equity-equivalent loss
        # from the aggregate beta-weighted delta exposure under a
        # benchmark move of shock_pct%.
        approx_loss = total_beta_weighted_delta_shares * (shock_frac / 100.0)
        stress_results[f"{benchmark} {shock_pct:+g}%"] = round(approx_loss, 2)

    regime_flags: List[str] = []
    regime_state = "normal"

    if current_drawdown_pct >= regime_severe_drawdown:
        regime_state = "severe"
        regime_flags.append(
            f"Portfolio drawdown {current_drawdown_pct:.1f}% >= severe threshold "
            f"{regime_severe_drawdown:.1f}% — pause new discretionary premium selling "
            f"unless a written exception is documented (Stage 14)."
        )
    elif current_drawdown_pct >= regime_review_drawdown:
        regime_state = "review"
        regime_flags.append(
            f"Portfolio drawdown {current_drawdown_pct:.1f}% >= review threshold "
            f"{regime_review_drawdown:.1f}% — reduce new position sizes 25-50%, "
            f"prioritize closing weak positions (Stage 14)."
        )

    if current_vix is not None and current_vix >= regime_vix_threshold:
        regime_state = "severe" if regime_state == "normal" else regime_state
        regime_flags.append(f"VIX {current_vix:.1f} >= {regime_vix_threshold:.1f} — hard review trigger (Stage 14).")
    elif current_vix is None:
        regime_flags.append(
            "current_vix not set in config.yaml — VIX circuit breaker is disabled. "
            "This pipeline has no free VIX feed; set it manually before relying on Stage 14."
        )

    if hedge_status not in ("hedged", "partially_hedged", "unhedged"):
        regime_flags.append(
            f"portfolio.hedge_status={hedge_status!r} is not one of "
            "hedged/partially_hedged/unhedged — Stage 13 requires an explicit choice."
        )

    return {
        "authorized_count": len(authorized),
        "exposures": exposures,
        "total_beta_weighted_delta_shares": round(total_beta_weighted_delta_shares, 2),
        "stress_results": stress_results,
        "hedge_status": hedge_status,
        "current_drawdown_pct": current_drawdown_pct,
        "current_vix": current_vix,
        "regime_state": regime_state,
        "regime_flags": regime_flags,
        "portfolio_value": portfolio_value,
    }


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------


def write_report(
    results: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    portfolio_stress: Optional[Dict[str, Any]] = None,
    batch_stats: Optional[Dict[str, Any]] = None,
) -> None:
    report_dir = Path(cfg.get("report_dir", "reports"))
    report_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = report_dir / f"actionable_report_{ts}.csv"
    md_path = report_dir / f"actionable_report_{ts}.md"

    df = pd.DataFrame(results)

    # CSV
    df.to_csv(csv_path, index=False)

    # Markdown
    lines: List[str] = []
    lines.append("# Options Trading Plan v3.3 — Actionable Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now().isoformat()}")
    lines.append("")

    if batch_stats:
        lines.append("## Batch Health")
        lines.append("")
        lines.append(f"- Universe size: {batch_stats.get('universe_size')}")
        lines.append(
            f"- Symbols with a usable options chain: {batch_stats.get('symbols_with_options')} "
            f"({batch_stats.get('completeness_pct', 0):.1f}%)"
        )
        if batch_stats.get("degraded"):
            lines.append(
                f"- ⚠️ **Degraded run**: completeness below configured "
                f"min_batch_completeness_pct={batch_stats.get('min_batch_completeness_pct')}%. "
                "This may indicate Yahoo Finance rate limiting or a partial outage — "
                "treat this run's REJECT/DATA INCOMPLETE symbols with caution and "
                "consider re-running before trusting the results."
            )
        lines.append("")

    if portfolio_stress:
        lines.append("## Portfolio-Level Check (Stages 11-14)")
        lines.append("")
        lines.append(
            "This section is a manually-configured, illustrative check "
            "(no free VIX/drawdown feed exists) — see plan.md Section 7 for scope."
        )
        lines.append("")
        lines.append(f"- AUTHORIZED candidates today: {portfolio_stress.get('authorized_count')}")
        lines.append(
            f"- Illustrative beta-weighted delta exposure (1 contract each, shares-equivalent): "
            f"{portfolio_stress.get('total_beta_weighted_delta_shares')}"
        )
        for label, loss in (portfolio_stress.get("stress_results") or {}).items():
            lines.append(f"- Stress {label}: approx {loss} shares-equivalent P/L impact")
        lines.append(f"- Hedge status: {portfolio_stress.get('hedge_status')}")
        lines.append(f"- Current drawdown: {portfolio_stress.get('current_drawdown_pct')}%")
        lines.append(f"- Current VIX: {portfolio_stress.get('current_vix')}")
        lines.append(f"- Regime state: **{portfolio_stress.get('regime_state')}**")
        for flag in portfolio_stress.get("regime_flags", []):
            lines.append(f"  - ⚠️ {flag}")
        lines.append("")

    if df.empty:
        lines.append("No results.")
    else:
        counts = df["decision_label"].value_counts()

        lines.append("## Summary")
        lines.append("")
        for label, count in counts.items():
            lines.append(f"- {label}: {count}")
        lines.append("")

        lines.append("## Results")
        lines.append("")
        lines.append(
            "| Symbol | Decision | Strategy | Contract | Strike | Exp | DTE | Delta | IV Rank | VRP Ratio | Score | Reasons |"
        )
        lines.append("|---|---|---|---|---:|---|---:|---:|---:|---:|---:|---|")

        for r in results:
            reasons = r.get("reasons", [])
            if isinstance(reasons, list):
                reasons_text = "; ".join(str(x) for x in reasons)
            else:
                reasons_text = str(reasons)

            reasons_text = reasons_text.replace("|", "/")

            contract_id = r.get("contract_id") or ""
            if len(contract_id) > 25:
                contract_display = contract_id[:25] + "…"
            else:
                contract_display = contract_id

            lines.append(
                "| {symbol} | {decision} | {strategy} | {contract} | {strike} | {exp} | {dte} | {delta} | {iv_rank} | {vrp} | {score} | {reasons} |".format(
                    symbol=r.get("symbol", ""),
                    decision=r.get("decision_label", ""),
                    strategy=r.get("strategy", ""),
                    contract=contract_display,
                    strike=r.get("strike", ""),
                    exp=r.get("expiration", ""),
                    dte=r.get("dte", ""),
                    delta=r.get("delta", ""),
                    iv_rank=r.get("iv_rank", ""),
                    vrp=r.get("vrp_ratio", ""),
                    score=r.get("score", ""),
                    reasons=reasons_text,
                )
            )

        lines.append("")
        lines.append("## Detailed Candidate Templates")
        lines.append("")

        for r in results:
            if r.get("decision_label") not in ("🟢 AUTHORIZED", "🟡 WAIT"):
                continue

            lines.append(f"### {r.get('symbol')} — {r.get('decision_label')}")
            lines.append("")
            lines.append(f"- Strategy: {r.get('strategy')}")
            lines.append(f"- Contract: {r.get('contract_id')}")
            lines.append(f"- Strike: {r.get('strike')}")
            lines.append(f"- Expiration: {r.get('expiration')}")
            lines.append(f"- DTE: {r.get('dte')}")
            lines.append(f"- Delta: {r.get('delta')}")
            lines.append(f"- IV: {r.get('iv')}")
            lines.append(f"- IV Rank: {r.get('iv_rank')}")
            lines.append(f"- VRP Ratio: {r.get('vrp_ratio')}")
            lines.append(f"- OI: {r.get('open_interest')}")
            lines.append(f"- Volume: {r.get('volume')}")
            lines.append(f"- Spread %: {r.get('spread_pct')}")
            lines.append(f"- Net Premium: {r.get('net_premium')}")
            lines.append(f"- Premium / Cost Ratio: {r.get('premium_cost_ratio')}")
            lines.append(f"- Score: {r.get('score')}")

            reasons = r.get("reasons", [])
            if isinstance(reasons, list) and reasons:
                lines.append("- Notes:")
                for reason in reasons:
                    lines.append(f"  - {reason}")

            lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")

    logging.info("Report written: %s", csv_path)
    logging.info("Report written: %s", md_path)
