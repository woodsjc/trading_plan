CONFIG = {
    "database_path": "options_info.db",
    "report_dir": "reports",
    # If symbols is empty, the script reads symbols_file
    "symbols": [],
    "symbols_file": "symbols.csv",
    # Data collection
    "max_workers": 4,
    "history_period": "1y",
    "min_dte_fetch": 30,
    "max_dte_fetch": 45,
    # Delay (seconds) inserted between yfinance calls for a given symbol, and
    # used as the base for exponential backoff on failure. Helps avoid
    # tripping Yahoo's unofficial rate limits across a ~100-symbol batch.
    "request_delay_seconds": 0.05,
    "max_retries": 3,
    "retry_backoff_seconds": 1.0,
    # If fewer than this fraction of the universe returns a usable options
    # chain, the run is flagged as degraded in the log / report summary
    # (rate limiting / partial outage protection).
    "min_batch_completeness_pct": 80.0,
    # Evidence handling
    # True: missing required data becomes DATA INCOMPLETE
    # False: missing data allows planning-only evaluation
    "strict_evidence": False,
    # v3.3 requires earnings date. Keep True for stricter evidence.
    "require_earnings_date": True,
    # Stage 0 freshness gate: a collected row older than this many hours
    # is treated as stale and pushes the symbol to DATA INCOMPLETE (or
    # planning-only, depending on strict_evidence).
    "max_data_age_hours": 24,
    # If True, the script can estimate IV Rank using historical realized
    # volatility whenever there isn't yet enough locally-accrued IV30 history.
    # This RV-proxy IV Rank is NOT true IV Rank and downgrades symbols to
    # planning-only. It is superseded automatically once enough daily IV30
    # snapshots have accrued in iv_history (see min_iv_history_days below).
    "allow_estimated_iv_rank": True,
    # Minimum number of accrued daily IV30 observations (in the iv_history
    # table) required before IV Rank is computed from real historical IV
    # instead of the RV proxy. iv_history is appended to on every run and is
    # never dropped, unlike underlying_metrics/options_contracts.
    "min_iv_history_days": 100,
    # Risk-free rate
    "fred_api_key": "",
    "fred_series": "DTB3",
    "allow_default_rf_rate": True,
    "default_rf_rate": 0.05,
    # Date the default_rf_rate value above was last manually reviewed/updated.
    # get_fred_rate() warns loudly (and the run is marked rf_estimated) when
    # falling back to this default, including how many days stale it is.
    "default_rf_rate_asof": "2026-01-01",
    # Optional manual IV Rank file
    "iv_rank_override_file": "iv_rank_overrides.csv",
    # Covered-call behavior
    "allow_buy_write": True,
    "portfolio": {
        "value": 100000,
        "max_position_pct": 5,
        "enforce_sizing": False,
        "holdings": {},
        # Example:
        # "holdings": {"AAPL": 100, "MSFT": 200},
        # --- Portfolio-level stages (11-14) ---
        # These require inputs this pipeline cannot fetch for free (VIX,
        # realized drawdown, per-symbol beta-weighted delta history), so they
        # are supplied manually here and checked as a post-processing pass
        # over the day's AUTHORIZED candidates rather than fully automated.
        "benchmark": "SPY",
        "benchmark_beta": 1.0,
        "current_vix": None,  # set manually; None disables the VIX circuit breaker
        "current_drawdown_pct": 0.0,  # portfolio drawdown from peak, as a positive percent
        "hedge_status": "unhedged",  # one of: hedged, partially_hedged, unhedged
        "stress_shocks_pct": [
            -20,
            -30,
        ],  # SPY shock scenarios to report against beta-weighted delta
        "regime_review_drawdown_pct": 10.0,
        "regime_severe_drawdown_pct": 15.0,
        "regime_vix_threshold": 30.0,
    },
    "screen": {
        "min_market_cap": 2_000_000_000,
        "min_avg_volume": 500_000,
        "dte_min": 30,
        "dte_max": 40,
        "delta_min": 0.15,
        "delta_max": 0.30,
        "min_open_interest": 250,
        "min_volume": 10,
        "max_spread_pct": 0.10,
        "min_premium_cost_ratio": 5.0,
        "iv_rank_min": 30.0,
        "vrp_ratio_min": 1.05,
        "earnings_buffer_days": 7,
    },
}
