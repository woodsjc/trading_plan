import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from parsing import parse_date, safe_float


def init_db(conn: sqlite3.Connection) -> None:
    """
    Drop and recreate the point-in-time snapshot tables (underlying_metrics,
    options_contracts) — these represent "as of this run" state and should
    be fully refreshed each time.

    iv_history is intentionally NOT dropped here. It accrues one row per
    symbol per day so that a real historical-IV-based IV Rank can
    eventually be computed instead of relying on the RV-proxy fallback
    indefinitely. See load_iv30_history() / get_iv_rank().
    """
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS underlying_metrics")
    cur.execute("DROP TABLE IF EXISTS options_contracts")

    cur.execute("""
        CREATE TABLE underlying_metrics (
            symbol TEXT PRIMARY KEY,
            last_updated TEXT,
            price REAL,
            average_volume REAL,
            market_cap REAL,
            beta REAL,
            earnings_date TEXT,
            ex_div_date TEXT,
            div_amount REAL,
            iv30 REAL,
            iv_rank REAL,
            iv_rank_source TEXT,
            iv_rank_estimated INTEGER,
            rv20d REAL,
            rv30d REAL,
            vrp_pp REAL,
            vrp_ratio REAL,
            rf_rate REAL,
            rf_source TEXT,
            rf_estimated INTEGER,
            notes TEXT
        )
        """)

    cur.execute("""
        CREATE TABLE options_contracts (
            contract_id TEXT PRIMARY KEY,
            symbol TEXT,
            expiration TEXT,
            dte INTEGER,
            strike REAL,
            type TEXT,
            bid REAL,
            ask REAL,
            midpoint REAL,
            spread REAL,
            spread_pct REAL,
            volume REAL,
            open_interest REAL,
            iv REAL,
            delta REAL,
            extrinsic_value REAL,
            last_updated TEXT
        )
        """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS iv_history (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            iv30 REAL,
            rv20d REAL,
            rv30d REAL,
            PRIMARY KEY (symbol, date)
        )
        """)

    conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    """
    Open (creating the parent directory if needed) and initialize the
    snapshot tables for db_path. init_db() drops/recreates
    underlying_metrics/options_contracts and ensures iv_history exists.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    init_db(conn)
    return conn


def save_underlying_rows(conn: sqlite3.Connection, underlying_rows: List[Dict[str, Any]]) -> None:
    if not underlying_rows:
        return

    pd.DataFrame(underlying_rows).to_sql(
        "underlying_metrics",
        conn,
        if_exists="append",
        index=False,
    )
    conn.commit()


def save_option_contracts(conn: sqlite3.Connection, contract_frames: List[pd.DataFrame]) -> None:
    if not contract_frames:
        return

    contracts = pd.concat(contract_frames, ignore_index=True)
    contracts.to_sql(
        "options_contracts",
        conn,
        if_exists="append",
        index=False,
    )
    conn.commit()


def load_underlying_metrics(conn: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM underlying_metrics WHERE symbol = ?",
        conn,
        params=(symbol,),
    )


def load_options_contracts(conn: sqlite3.Connection, symbol: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM options_contracts WHERE symbol = ?",
        conn,
        params=(symbol,),
    )


def load_iv30_history(db_path: Path) -> Dict[str, "pd.Series"]:
    """
    Load the accrued iv_history table (never dropped between runs) into a
    per-symbol Series of past IV30 observations, used to compute a real
    historical-IV-based IV Rank once enough daily snapshots exist.

    Returns an empty dict if the DB/table doesn't exist yet (first run).
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return {}

    try:
        conn = sqlite3.connect(db_path)
        try:
            df = pd.read_sql_query("SELECT symbol, date, iv30 FROM iv_history", conn)
        finally:
            conn.close()
    except Exception as exc:
        logging.info("No usable iv_history yet (%s) — will use RV proxy/manual override.", exc)
        return {}

    if df.empty:
        return {}

    out: Dict[str, pd.Series] = {}
    for sym, group in df.groupby("symbol"):
        series = pd.to_numeric(group.sort_values("date")["iv30"], errors="coerce").dropna()
        if not series.empty:
            out[str(sym)] = series.reset_index(drop=True)

    return out


def upsert_iv_history(conn: sqlite3.Connection, underlying_rows: List[Dict[str, Any]]) -> None:
    """
    Append today's IV30/RV snapshot per symbol into the never-dropped
    iv_history table. INSERT OR REPLACE keyed on (symbol, date) makes this
    safe to re-run multiple times on the same day without duplicating rows.
    """
    if not underlying_rows:
        return

    rows = []
    for u in underlying_rows:
        iv30 = safe_float(u.get("iv30"))
        if iv30 is None:
            continue

        last_updated = u.get("last_updated")
        d = parse_date(last_updated) or date.today()

        rows.append(
            (
                u.get("symbol"),
                d.isoformat(),
                iv30,
                safe_float(u.get("rv20d")),
                safe_float(u.get("rv30d")),
            )
        )

    if not rows:
        return

    cur = conn.cursor()
    cur.executemany(
        """
        INSERT OR REPLACE INTO iv_history (symbol, date, iv30, rv20d, rv30d)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    logging.info("iv_history: upserted %s row(s) for today", len(rows))
