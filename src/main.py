import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from config import CONFIG
from database import (
    connect,
    load_iv30_history,
    save_option_contracts,
    save_underlying_rows,
    upsert_iv_history,
)
from options_plan import (
    base_result,
    evaluate_portfolio_stress,
    evaluate_symbol,
    fetch_price_history,
    load_iv_rank_overrides,
    process_symbol,
    write_report,
)
from parsing import safe_float
from safe_rate import get_fred_rate
from symbols import load_symbols


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = CONFIG
    symbols = load_symbols(config)
    if not symbols:
        logging.error("No symbols found. Add symbols to config.py or universe.csv.")
        sys.exit(1)

    logging.info("Universe size: %s", len(symbols))

    rf_meta = get_fred_rate(config)
    logging.info(
        "Risk-free rate: %s source=%s estimated=%s",
        rf_meta.get("rate"),
        rf_meta.get("source"),
        rf_meta.get("estimated"),
    )

    max_retries = int(config.get("max_retries", 3))
    retry_backoff_seconds = float(config.get("retry_backoff_seconds", 1.0))

    history = fetch_price_history(
        symbols,
        config.get("history_period", "1y"),
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
    )

    overrides = load_iv_rank_overrides(
        config.get("iv_rank_override_file", "iv_rank_overrides.csv")
    )

    db_path = Path(config.get("database_path", "options_v33.db"))
    iv30_history_map = load_iv30_history(db_path)
    logging.info(
        "Loaded iv_history for %s symbol(s); min_iv_history_days=%s",
        len(iv30_history_map),
        config.get("min_iv_history_days", 100),
    )

    underlying_rows: List[Dict[str, Any]] = []
    contract_frames: List[pd.DataFrame] = []

    max_workers = int(config.get("max_workers", 4))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                process_symbol,
                symbol,
                config,
                history,
                overrides,
                rf_meta,
                iv30_history_map,
            ): symbol
            for symbol in symbols
        }

        for future in as_completed(futures):
            symbol = futures[future]

            try:
                result = future.result()

                underlying = result.get("underlying")
                options = result.get("options")

                if underlying:
                    underlying_rows.append(underlying)

                if options is not None and not options.empty:
                    contract_frames.append(options)

            except Exception as exc:
                logging.exception("Future failed for %s: %s", symbol, exc)

    # Batch completeness check: a low fraction of symbols returning a
    # usable options chain is a signal of rate limiting / partial outage
    # rather than every symbol legitimately lacking listed options.
    symbols_with_options = sum(
        1 for df in contract_frames if df is not None and not df.empty
    )
    completeness_pct = (symbols_with_options / len(symbols)) * 100.0 if symbols else 0.0
    min_batch_completeness_pct = (
        safe_float(config.get("min_batch_completeness_pct", 80.0)) or 80.0
    )
    degraded = completeness_pct < min_batch_completeness_pct

    batch_stats = {
        "universe_size": len(symbols),
        "symbols_with_options": symbols_with_options,
        "completeness_pct": completeness_pct,
        "min_batch_completeness_pct": min_batch_completeness_pct,
        "degraded": degraded,
    }

    if degraded:
        logging.warning(
            "Batch completeness %.1f%% is below min_batch_completeness_pct=%.1f%% "
            "(%s/%s symbols returned a usable options chain). This run may be "
            "degraded by rate limiting or a partial data outage — treat REJECT/"
            "DATA INCOMPLETE results with extra caution.",
            completeness_pct,
            min_batch_completeness_pct,
            symbols_with_options,
            len(symbols),
        )
    else:
        logging.info(
            "Batch completeness OK: %.1f%% (%s/%s symbols)",
            completeness_pct,
            symbols_with_options,
            len(symbols),
        )

    conn = connect(db_path)

    save_underlying_rows(conn, underlying_rows)
    save_option_contracts(conn, contract_frames)

    upsert_iv_history(conn, underlying_rows)

    results: List[Dict[str, Any]] = []

    for symbol in symbols:
        try:
            res = evaluate_symbol(symbol, config, conn)
            results.append(res)
        except Exception as exc:
            logging.exception("Evaluation failed for %s", symbol)
            r = base_result(symbol)
            r["reasons"].append(f"Evaluation exception: {exc}")
            results.append(r)

    portfolio_stress = evaluate_portfolio_stress(results, config)

    if portfolio_stress.get("regime_state") != "normal":
        logging.warning(
            "Portfolio regime state: %s — %s",
            portfolio_stress.get("regime_state"),
            "; ".join(portfolio_stress.get("regime_flags", [])),
        )

    write_report(
        results, config, portfolio_stress=portfolio_stress, batch_stats=batch_stats
    )

    conn.close()

    logging.info("Done.")


if __name__ == "__main__":
    main()
