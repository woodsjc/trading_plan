import logging
import time
import warnings
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.optimize import brentq
from scipy.stats import norm

warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)

try:
    from curl_cffi import requests as c_requests

    session = c_requests.Session(impersonate="chrome")
except Exception:
    session = None

EARNINGS_WINDOW_DAYS = 21
EXCLUDE_UNKNOWN_EARNINGS = False

TIER1_STOCKS = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "GOOG",
    "META",
    "BRK-B",
    "AVGO",
    "TSLA",
    "LLY",
    "JPM",
    "UNH",
    "V",
    "MA",
    "COST",
    "HD",
    "PG",
    "NFLX",
    "ABBV",
    "CRM",
    "BAC",
    "XOM",
    "CVX",
    "KO",
    "PEP",
    "TMO",
    "CSCO",
    "ACN",
    "WMT",
    "MRK",
    "ABT",
    "ADBE",
    "WFC",
    "MCD",
    "AMD",
    "NOW",
    "IBM",
    "DIS",
    "GE",
    "CAT",
    "TXN",
    "INTU",
    "VZ",
    "AMGN",
    "PFE",
    "ISRG",
    "GS",
    "BKNG",
    "AMAT",
    "DHR",
    "CMCSA",
    "HON",
    "LOW",
    "RTX",
    "MS",
    "BLK",
    "COP",
    "EL",
    "BA",
    "DE",
    "SYK",
    "SCHW",
    "AXP",
    "GILD",
    "LRCX",
    "CVS",
    "TJX",
    "REGN",
    "ADI",
    "CI",
    "CB",
    "PGR",
    "AMT",
    "BSX",
    "ZTS",
    "BDX",
    "TMUS",
    "ETN",
    "MO",
    "SHW",
    "APD",
]
TIER1_INDEX = ["SPY", "QQQ", "IWM", "DIA", "XLE", "XLF", "XLK", "GLD", "SLV", "TLT", "XLV", "RSP"]
ALL_SYMBOLS = TIER1_STOCKS + TIER1_INDEX


def parse_date(s: str) -> date:
    """Parse a YYYY-MM-DD string into a date (tz-aware parse, then dropped to a date)."""
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC).date()


def bs_price(S, K, T, r, sigma, opt_type="call"):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if opt_type == "call":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    else:
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def calc_iv(mkt_price, S, K, T, r=0.045, opt_type="call"):
    try:
        if mkt_price <= 0 or T <= 0 or S <= 0:
            return np.nan
        # intrinsic bound
        intrinsic = max(S - K, 0) if opt_type == "call" else max(K - S, 0)
        if mkt_price < intrinsic * 0.95:
            return np.nan
        return brentq(lambda s: bs_price(S, K, T, r, s, opt_type) - mkt_price, 0.01, 5.0, maxiter=200)
    except Exception:
        return np.nan


def get_next_earnings_date(ticker):
    """Return the next upcoming earnings date (datetime.date) or None if it can't be found."""
    today = datetime.now(UTC).date()
    tk = yf.Ticker(ticker, session=session)
    dates = []

    # Source 1: calendar (fast, usually has the next confirmed/estimated date)
    try:
        cal = tk.calendar
        raw = []
        if isinstance(cal, dict):
            raw = cal.get("Earnings Date") or []
        elif isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
            raw = cal.loc["Earnings Date"].dropna().tolist()
        if not isinstance(raw, (list, tuple)):
            raw = [raw]
        for d in raw:
            try:
                dates.append(pd.Timestamp(d).date())
            except Exception as e:
                logger.debug("%s: bad calendar earnings date %r: %s", ticker, d, e)
    except Exception as e:
        logger.debug("%s: calendar lookup failed: %s", ticker, e)

    # Source 2: earnings-dates table (fallback if calendar gave nothing in the future)
    if not any(d >= today for d in dates):
        try:
            ed = tk.get_earnings_dates(limit=8)
            if ed is not None and not ed.empty:
                for ts in ed.index:
                    try:
                        dates.append(pd.Timestamp(ts).date())
                    except Exception as e:
                        logger.debug("%s: bad earnings-dates entry %r: %s", ticker, ts, e)
        except Exception as e:
            logger.debug("%s: get_earnings_dates failed: %s", ticker, e)

    future = sorted(d for d in dates if d >= today)
    return future[0] if future else None


def get_hv_and_price(ticker):
    try:
        hist = yf.Ticker(ticker, session=session).history(period="1y", auto_adjust=True)
        if hist.empty or len(hist) < 35:
            return np.nan, np.nan, np.nan, np.nan, hist
        close = hist["Close"]
        log_ret = np.log(close / close.shift(1)).dropna()
        hv10 = log_ret.tail(10).std() * np.sqrt(252) * 100
        hv20 = log_ret.tail(20).std() * np.sqrt(252) * 100
        hv30 = log_ret.tail(30).std() * np.sqrt(252) * 100
        last_price = float(close.iloc[-1])
        return float(hv10), float(hv20), float(hv30), last_price, hist
    except Exception:
        return np.nan, np.nan, np.nan, np.nan, pd.DataFrame()


def get_atm_contract_info(ticker, last_price, hist_1y):
    try:
        tk = yf.Ticker(ticker, session=session)
        exps = tk.options
        if not exps:
            return None
        today = datetime.now(UTC).date()
        # find 25-50 DTE expiry for IV30
        candidates = []
        for exp in exps:
            d = parse_date(exp)
            days = (d - today).days
            if 20 <= days <= 55:
                candidates.append((exp, days))
        if not candidates:
            candidates = [(exps[0], 30)]
        candidates.sort(key=lambda x: abs(x[1] - 30))
        exp_str, _ = candidates[0]
        chain = tk.option_chain(exp_str)
        calls = chain.calls
        if calls.empty:
            return None
        atm = calls.iloc[(abs(calls["strike"] - last_price)).argsort()[:1]].iloc[0]
        strike = float(atm["strike"])
        # Build OCC symbol for history download: e.g. SPY260930C00770880 (strike*1000 padded 8)
        # yfinance expects format like "SPY260930C00770000" without dash
        # strike*1000 -> int
        strike_int = round(strike * 1000)
        exp_YYMMDD = parse_date(exp_str).strftime("%y%m%d")
        # Handle tickers with 1-5 chars, OCC pads to 6
        occ = f"{ticker.upper():<6}{exp_YYMMDD}C{strike_int:08d}"
        occ = occ.replace(" ", "")
        # For BRK-B etc, yfinance needs dash format sometimes
        opt_ticker_yf = occ
        return {
            "exp_str": exp_str,
            "strike": strike,
            "occ": occ,
            "opt_ticker_yf": opt_ticker_yf,
            "candidates": candidates,
        }
    except Exception as e:
        print(f" contract info error {ticker}: {e}")
        return None


def get_iv_rank(ticker, current_iv, last_price, hist_1y, contract_info):
    try:
        if contract_info is None:
            return np.nan, np.nan, np.nan, np.nan, np.nan

        opt_symbol = contract_info["opt_ticker_yf"]
        exp_str = contract_info["exp_str"]
        strike = contract_info["strike"]

        # Download 1 year of this specific option contract history
        # This is the key to get 52-week IV range without paid API
        opt_hist = yf.Ticker(opt_symbol, session=session).history(period="1y", auto_adjust=True)
        if opt_hist.empty or len(opt_hist) < 20:
            # fallback: try with dash format for special tickers
            # yfinance sometimes needs SPY260930C00770 etc - try without leading zeros
            # If still empty, we can't calc rank
            return np.nan, np.nan, np.nan, np.nan, np.nan

        # Align dates
        merged = pd.merge(
            hist_1y[["Close"]].rename(columns={"Close": "Underlying"}),
            opt_hist[["Close"]].rename(columns={"Close": "OptClose"}),
            left_index=True,
            right_index=True,
            how="inner",
        )
        if len(merged) < 20:
            return np.nan, np.nan, np.nan, np.nan, np.nan

        iv_series = []
        today = datetime.now(UTC).date()
        exp_date = parse_date(exp_str)
        for idx, row in merged.tail(252).iterrows():
            S = float(row["Underlying"])
            opt_price = float(row["OptClose"])
            # T = time to expiry on that historical date
            hist_date = idx.date() if hasattr(idx, "date") else today
            T = (exp_date - hist_date).days / 365
            if T <= 0.01:
                continue
            iv = calc_iv(opt_price, S, strike, T, opt_type="call")
            if not np.isnan(iv):
                iv_series.append(iv * 100)

        if len(iv_series) < 20:
            return np.nan, np.nan, np.nan, np.nan, np.nan

        iv_series = np.array(iv_series)
        iv_low = float(np.min(iv_series))
        iv_high = float(np.max(iv_series))
        if iv_high == iv_low:
            return 0.0, iv_low, iv_high, 50.0, len(iv_series)

        iv_rank = (current_iv - iv_low) / (iv_high - iv_low) * 100
        # IV Percentile: % of days IV was below current
        iv_perc = float((iv_series < current_iv).sum() / len(iv_series) * 100)

        return float(np.clip(iv_rank, 0, 100)), iv_low, iv_high, iv_perc, len(iv_series)
    except Exception:
        # print(f"IV Rank error {ticker}: {e}")
        return np.nan, np.nan, np.nan, np.nan, np.nan


def get_iv30_interp_and_nearest(ticker, last_price, contract_candidates):
    try:
        tk = yf.Ticker(ticker, session=session)
        today = datetime.now(UTC).date()
        # get two expiries bracketing 30
        lower = upper = None
        for exp, days in sorted(contract_candidates, key=lambda x: x[1]):
            if days <= 30:
                lower = (exp, days)
            if days >= 30 and upper is None:
                upper = (exp, days)
        if lower is None:
            lower = contract_candidates[0]
        if upper is None:
            upper = contract_candidates[-1]

        def iv_for_exp(exp):
            chain = tk.option_chain(exp)
            calls = chain.calls
            c = calls.iloc[(abs(calls["strike"] - last_price)).argsort()[:1]].iloc[0]
            iv = c.get("impliedVolatility", np.nan)
            if pd.isna(iv) or iv < 0.01:
                T = (parse_date(exp) - today).days / 365
                mid = (c["bid"] + c["ask"]) / 2 if c["bid"] > 0 else c["lastPrice"]
                iv = calc_iv(mid, last_price, c["strike"], T, "call")
            return float(iv) if not pd.isna(iv) else np.nan

        iv_low = iv_for_exp(lower[0])
        iv_up = iv_for_exp(upper[0])
        if pd.isna(iv_low) and pd.isna(iv_up):
            return np.nan, np.nan, "", ""
        if pd.isna(iv_low):
            return iv_up * 100, iv_up * 100, f"{upper[1]:.0f}", upper[0]
        if pd.isna(iv_up):
            return iv_low * 100, iv_low * 100, f"{lower[1]:.0f}", lower[0]

        if lower[0] == upper[0]:
            return iv_low * 100, iv_low * 100, f"{lower[1]:.0f}", lower[0]

        w = (30 - lower[1]) / (upper[1] - lower[1]) if upper[1] != lower[1] else 0
        iv30 = iv_low + w * (iv_up - iv_low)
        iv_nearest = iv_low if abs(lower[1] - 30) < abs(upper[1] - 30) else iv_up
        return float(iv30 * 100), float(iv_nearest * 100), f"{lower[1]:.0f}/{upper[1]:.0f}", f"{lower[0]}/{upper[0]}"
    except Exception:
        return np.nan, np.nan, "", ""


results = []
excluded = []
today_utc = datetime.now(UTC).date()
earnings_cutoff = today_utc + timedelta(days=EARNINGS_WINDOW_DAYS)

print(
    f"Scanning {len(ALL_SYMBOLS)} symbols for VRP + IV Rank "
    f"(skipping stocks with earnings on or before {earnings_cutoff}; ~2-3 sec per ticker)...\n"
)
for t in ALL_SYMBOLS:
    # --- Earnings filter (stocks only; ETFs/indexes have no earnings) ---
    next_earn = None
    days_to_earn = np.nan
    if t not in TIER1_INDEX:
        next_earn = get_next_earnings_date(t)
        if next_earn is not None:
            days_to_earn = (next_earn - today_utc).days
            if next_earn <= earnings_cutoff:
                print(f"{t}: EARNINGS {next_earn} (in {days_to_earn}d) - SKIPPED")
                excluded.append({"Ticker": t, "Next_Earnings": next_earn, "Days_To_Earnings": days_to_earn})
                time.sleep(0.2)
                continue
        elif EXCLUDE_UNKNOWN_EARNINGS:
            print(f"{t}: EARNINGS DATE UNKNOWN - SKIPPED")
            excluded.append({"Ticker": t, "Next_Earnings": None, "Days_To_Earnings": np.nan})
            time.sleep(0.2)
            continue
        else:
            print(f"{t}: warning - earnings date unknown, keeping")

    hv10, hv20, hv30, price, hist_1y = get_hv_and_price(t)
    if np.isnan(price):
        print(f"{t}: NO PRICE")
        continue
    cinfo = get_atm_contract_info(t, price, hist_1y)
    if cinfo is None:
        print(f"{t}: NO CONTRACT")
        continue
    iv30_interp, iv30_nearest, dtes, exps = get_iv30_interp_and_nearest(t, price, cinfo["candidates"])
    if np.isnan(iv30_interp):
        print(f"{t}: NO IV30")
        time.sleep(0.3)
        continue

    iv_rank, iv_low_52w, iv_high_52w, iv_perc, n_pts = get_iv_rank(t, iv30_interp, price, hist_1y, cinfo)

    vrp30 = iv30_interp / hv30 if hv30 > 0 else np.nan
    vrp20 = iv30_interp / hv20 if hv20 > 0 else np.nan

    results.append(
        {
            "Ticker": t,
            "Type": "ETF/INDEX" if t in TIER1_INDEX else "STOCK",
            "Price": round(price, 2),
            "Expiry": exps,
            "Next_Earnings": next_earn if next_earn is not None else "",
            "Days_To_Earnings": days_to_earn,
            "HV10": round(hv10, 2),
            "HV20": round(hv20, 2),
            "HV30": round(hv30, 2),
            "IV30": round(iv30_interp, 2),
            "IV30_nearest": round(iv30_nearest, 2),
            "IV_low_52w": round(iv_low_52w, 2) if not np.isnan(iv_low_52w) else np.nan,
            "IV_high_52w": round(iv_high_52w, 2) if not np.isnan(iv_high_52w) else np.nan,
            "IV_Rank": round(iv_rank, 1) if not np.isnan(iv_rank) else np.nan,
            "IV_Perc": round(iv_perc, 1) if not np.isnan(iv_perc) else np.nan,
            "VRP_30": round(vrp30, 2),
            "VRP_20": round(vrp20, 2),
            "Prem30": round(iv30_interp - hv30, 2),
        }
    )
    print(
        f"{t}: HV30 {hv30:.1f}% IV30 {iv30_interp:.1f}% VRP30 {vrp30:.2f} | IV Rank {iv_rank:.0f}% (Low {iv_low_52w:.1f}% High {iv_high_52w:.1f}%)"
    )
    time.sleep(0.4)

if excluded:
    pd.DataFrame(excluded).to_csv("earnings_excluded.csv", index=False)
    print(f"\nExcluded {len(excluded)} symbols for earnings -> earnings_excluded.csv")

df = pd.DataFrame(results)
if not df.empty:
    df_vrp = df[df["VRP_30"] > 1.2].sort_values("VRP_30", ascending=False)
    print(f"\n=== VRP_30 > 1.2 (Schwab matched, no earnings in next {EARNINGS_WINDOW_DAYS} days) ===")
    print(df_vrp.to_string(index=False))
else:
    print("No data")
