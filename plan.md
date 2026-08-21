# plan.md: Data Collection & Pipeline Architecture for Options Trading Plan v3.5

## 1. The Data Source Reality Check
**Is there a single datasource like Yahoo Finance that has *everything* already available?**
**No.** There is no single free or low-cost retail API that provides every data point required by v3.5 out-of-the-box.

*   **Yahoo Finance (`yfinance`)** is excellent for underlying fundamentals, current prices, dividends, earnings dates, and current options chains (Bid/Ask, IV, OI, Volume).
*   **However, Yahoo Finance DOES NOT provide:** Historical Implied Volatility (required to calculate IV Rank/Percentile), historical realized volatility (requires raw price history), Dealer Gamma Exposure (GEX), or volatility term structures.

**The Solution:** You must use a **hybrid data approach**. You will use Yahoo Finance for live underlying/contract data, and a specialized historical options data provider (like **Polygon.io**, **CBOE DataShop**, or **Orats**) to calculate the advanced volatility metrics (IV Rank, VRP, Term Structure).

---

## 2. Recommended Data Stack for Python

To process ~100 symbols efficiently, you need a robust, async-capable stack.

| Data Category | Recommended Source / API | Python Library / Tool | Notes |
| :--- | :--- | :--- | :--- |
| **Underlying & Fundamentals** | Yahoo Finance | `yfinance` | Price, Volume, Mkt Cap, Beta, Divs, Earnings. |
| **Live Options Chain** | Yahoo Finance / IBKR | `yfinance` / `ib_insync` | Bid/Ask, OI, Vol, IV, Greeks. (IBKR is better for live execution). |
| **Historical Options (for IV Rank)**| Polygon.io (Paid) / CBOE | `polygon-api-client` | **Crucial:** You need historical daily IV to calculate IV Rank/Percentile. |
| **Historical Prices (for RV)** | Yahoo Finance / Polygon | `yfinance` / `pandas` | Used to calculate 20D/30D Realized Volatility. |
| **Risk-Free Rate** | FRED (Federal Reserve) | `fredapi` | Free API for T-Bill yields (Stage 7 hurdle). |
| **GEX / Advanced Vol** | SpotGamma / Tier1Alpha / Unusual Whales | Custom API / Web Scraping | Optional but recommended for Stage 2.5 / Stage 10 modifiers. |
| **Data Storage** | Local Database | `SQLite` / `DuckDB` / `PostgreSQL` | **Do not use CSVs.** Use a DB for fast querying of 100 symbols. |

---

## 3. Batch Collection Architecture (100 Symbols)

To collect data for 100 symbols without hitting rate limits or timing out, use an asynchronous pipeline.

### 3.1 Pipeline Flow
1.  **Universe Definition:** Load the list of 100 symbols.
2.  **Async Fetching:** Use `asyncio` and `aiohttp` (or async wrappers for `yfinance`/`polygon`) to fetch data concurrently.
3.  **Data Normalization & Calculation:** Calculate RV, IV Rank, VRP, and Term Structure in `pandas`/`numpy`.
4.  **Database Ingestion:** Upsert the calculated data into a local `DuckDB` or `SQLite` database.
5.  **Gate Validation:** Run the "Stage 0: Data Completeness" check.

---

## 4. Mapping v3.5 Stages to the Data Pipeline

Here is exactly how the collected data satisfies the hard gates of the v3.5 plan.

### Stage 0: Data Completeness Gate (Hard Gate)
*Implementation:* Before running any trade logic, the Python script queries the database for the symbol. If any of the following are `NULL` or older than 24 hours, flag as `⚪ DATA INCOMPLETE`.
*   **Required Fields:** Price, Volume, Mkt Cap, Beta, Earnings Date, Div Date, IV30, IV Rank, RV_20D, RV_30D, Target Contract OI, Target Contract Vol, Target Contract Bid/Ask, T-Bill Yield.

### Stage 1: Underlying Screen
*   **Data Used:** Market Cap, Beta, Earnings Date, Dividend Date, Current Price.
*   **Logic:** Filter out if `Earnings Date` is within the next 60 days. Filter out if `Dividend Date` is within the option life.

### Stage 2: Volatility Screen (The Core Edge)
*   **Data Used:** IV30, IV Rank, RV_20D, RV_30D.
*   **Logic:**
    *   `IV_Rank >= 30` (Hard Gate).
    *   `VRP_Ratio = IV30 / RV_30D`. If `VRP_Ratio < 1.05`, trigger `🔴 REJECT`.
    *   Check multi-horizon: Ensure both `IV30 > RV_20D` and `IV30 > RV_30D`.
*   **IV Rank formula (corrected):** `IV_Rank = (IV30_today - min(IV30_history)) / (max(IV30_history) - min(IV30_history)) * 100`, computed over the accrued `iv_history` window once at least `min_iv_history_days` daily observations exist.
    *   An earlier version of this pipeline computed `mean(IV30_history < IV30_today) * 100` here — that is **IV Percentile** (the fraction of historical days IV was lower than today), not IV Rank. Percentile and Rank are related but numerically distinct measures and can diverge sharply, e.g. when most historical days cluster at one end of the range but a single outlier sets a far-away min or max: percentile can read ~99 while true rank reads ~25 for the same series. Because most retail brokerages label their volatility gauge "IV Rank" using the min/max formula, code and displayed dashboards should use that formula, not the percentile formula, to be comparable to what a brokerage shows. If a percentile-style measure is ever wanted again, it must be stored/labeled as `iv_percentile`, never as `iv_rank`.
    *   Until `min_iv_history_days` daily IV30 observations have accrued, the pipeline falls back to an **IV vs. realized-volatility proxy** (`estimated_rv_proxy`), which answers a different question ("is IV expensive vs. recent realized moves") than either IV Rank or IV Percentile ("is IV expensive vs. its own history"). This fallback is always marked `estimated=True` and forces the symbol to `🟡 WAIT` (planning-only) rather than `🟢 AUTHORIZED`.

### Stage 3: Catalyst Screen
*   **Data Used:** Earnings Date, Ex-Dividend Date.
*   **Logic:** Calculate days until earnings. If `Days_to_Earnings < DTE + 7`, trigger `🔴 REJECT` for standard trades.

### Stage 4 & 5: Contract & Liquidity Screen
*   **Data Used:** Options Chain (filtered for 30-40 DTE).
*   **Logic:**
    *   Filter strikes where `0.15 <= Delta <= 0.30`.
    *   Filter for `Open_Interest >= 250` (Hard Gate).
    *   Filter for `Daily_Volume >= 10` (Hard Gate).
    *   Calculate `Spread = Ask - Bid`. If `Spread / Midpoint > 0.10`, trigger `🔴 REJECT` (Hard Gate).
*   **IV normalization (corrected):** Yahoo occasionally returns IV in whole-percent form (e.g. `35.0` instead of `0.35`) for a given chain. The rescale decision (`÷100`) is made **once per symbol, at the chain level**, based on whether a majority (default 50%, configurable via `iv_rescale_majority_frac`) of that chain's valid non-null IV prints exceed `iv_rescale_threshold` (default `3.0`). A prior per-contract version rescaled any single contract with `iv > 3.0` in isolation — but a legitimately high-IV deep-OTM, small-cap, or event-driven contract can print IV > 300% for real, and the per-contract heuristic would silently corrupt that single strike's IV (and everything downstream: delta, extrinsic value, IV30, VRP) while leaving the rest of the chain untouched. Deciding at the chain level avoids punishing genuine outliers while still catching systemic units bugs. Every chain-level rescale is logged at INFO with the affected/total IV counts for audit.

### Stage 6: Premium Sufficiency
*   **Data Used:** Midpoint premium, Bid/Ask spread.
*   **Logic:**
    *   `Est_Cost = 2 * (Spread / 2)` (assuming half-spread slippage).
    *   `Net_Premium = Midpoint - Est_Cost`.
    *   If `Net_Premium / Est_Cost < 5`, trigger `🔴 REJECT`.

### Stage 7: Risk-Free Hurdle
*   **Data Used:** T-Bill Yield (from FRED API), Net Premium, Capital Reserved.
*   **Logic:** Calculate RF return for the DTE. If `Net_Premium < RF_Return`, trigger `🔴 REJECT`.

### Stage 9: Dividend / Assignment Risk
*   **Data Used:** Extrinsic Value, Upcoming Dividend.
*   **Logic:**
    *   `Extrinsic = Call_Price - max(0, Stock_Price - Strike)`.
    *   If `Extrinsic < Dividend_Amount` AND `Ex_Div_Date < Expiration`, flag high assignment risk.
*   **Dividend amount source (corrected):** `Dividend_Amount` must be the amount of the **single upcoming payment**, matching the v3.5 Stage 31 worked example (`extrinsic $1.00 < dividend $1.25`). An earlier version populated this from `info["dividendRate"]` / `trailingAnnualDividendRate` — both **annualized** figures (roughly 4x a single quarterly payment) — which overstates the relevant dividend and can misfire the Stage 9/31 check in either direction. The corrected source order is:
    1. The most recent actual per-share payment from `ticker.dividends` (real historical payout — the best available proxy for the next one absent a specifically announced amount). Stored with `div_amount_source = "last_actual_payment"`, `div_amount_estimated = False`.
    2. Fallback only if dividend history is unavailable: `annual_rate / 4` (assumes quarterly payment frequency). Stored with `div_amount_source = "annual_rate_div_4_estimate"`, `div_amount_estimated = True`, and surfaced as a warning on any resulting candidate so it isn't silently trusted as precise.

---

## 5. Database Schema Design (For 100 Symbols)

**Table: `underlying_metrics` (Updated Daily/Pre-market)**
| Column | Type | v3.5 Stage |
| :--- | :--- | :--- |
| `symbol` | VARCHAR (PK) | All |
| `last_updated` | TIMESTAMP | Stage 0 |
| `price` | FLOAT | Stage 1 |
| `beta` | FLOAT | Stage 10 |
| `market_cap` | BIGINT | Stage 1 |
| `earnings_date` | DATE | Stage 3 |
| `ex_div_date` | DATE | Stage 9 |
| `div_amount` | FLOAT | Stage 9 — **single-payment estimate**, not annualized (see Stage 9 above) |
| `div_amount_source` | VARCHAR | Stage 9 — `last_actual_payment` / `annual_rate_div_4_estimate` / `missing` |
| `div_amount_estimated` | BOOLEAN | Stage 9 — flags the coarser fallback |
| `iv30` | FLOAT | Stage 2 |
| `iv_rank` | FLOAT | Stage 2 — **true min/max IV Rank** (or the labeled RV-proxy fallback; see Stage 2 above), not IV Percentile |
| `iv_rank_source` | VARCHAR | Stage 2 — `manual_override` / `historical_iv_rank` / `estimated_rv_proxy` / `missing` |
| `rv_20d` | FLOAT | Stage 2 |
| `rv_30d` | FLOAT | Stage 2 |
| `vrp_pp` | FLOAT | Stage 2 |
| `vrp_ratio` | FLOAT | Stage 2 |
| `rf_rate` | FLOAT | Stage 7 |

> **Collection scope:** Only fetch/store expirations in the **30–40 DTE** window. Expirations outside this range should be skipped at fetch time (not just filtered later), to reduce API calls and keep the table lean for a ~100-symbol universe.

**Table: `options_contracts` (Updated Daily/Pre-market)**
| Column | Type | v3.5 Stage |
| :--- | :--- | :--- |
| `contract_id` | VARCHAR (PK) | Stage 4 |
| `symbol` | VARCHAR (FK) | Stage 4 |
| `expiration` | DATE | Stage 4 |
| `dte` | INT | Stage 4 |
| `strike` | FLOAT | Stage 4 |
| `type` | VARCHAR ('C'/'P')| Stage 4 |
| `bid` | FLOAT | Stage 5 |
| `ask` | FLOAT | Stage 5 |
| `midpoint` | FLOAT | Stage 6 |
| `delta` | FLOAT | Stage 4 — model-derived (Black-Scholes from quoted IV); expect divergence from a broker's live Greeks feed, especially for dividend payers, since this model has no dividend-yield adjustment |
| `iv` | FLOAT | Stage 4 — chain-level-rescale-corrected (see Stage 4/5 above) |
| `open_interest` | INT | Stage 5 |
| `volume` | INT | Stage 5 |
| `extrinsic_value`| FLOAT | Stage 9 |

---

## 6. Execution & "Do Nothing" Logic

The Python script should output a daily "Actionable Report" using the exact v3.5 Decision Labels.

```python
def run_v3_plan(symbol):
    data = db.fetch_symbol_data(symbol)

    # Stage 0: Data Completeness
    if not data.is_complete():
        return "⚪ DATA INCOMPLETE — DO NOT AUTHORIZE"

    # Stage 1 & 3: Underlying & Catalyst
    if data.earnings_date < data.target_dte + 7:
        return "🔴 REJECT: Earnings inside DTE"

    # Stage 2: Volatility (The Edge)
    if data.iv_rank < 30:
        return "🔴 REJECT: IV Rank < 30"
    if data.vrp_ratio < 1.05:
        return "🔴 REJECT: VRP Ratio < 1.05"

    # Stage 5: Liquidity
    valid_contracts = data.options[
        (data.options.dte.between(30, 40))
        & (data.options.oi >= 250)
        & (data.options.volume >= 10)
        & (data.options.spread_pct <= 0.10)
        & (data.options.delta.between(0.15, 0.30))
    ]

    if valid_contracts.empty:
        return "🔴 REJECT: No liquid contracts in delta range"

    # Stage 6 & 7: Premium & RF Hurdle
    best_contract = calculate_best_risk_adjusted_premium(valid_contracts)
    if best_contract.premium_to_cost_ratio < 5:
        return "🔴 REJECT: Premium insufficient for execution cost"
    if best_contract.net_premium < data.rf_return:
        return "🔴 REJECT: Fails Risk-Free Hurdle"

    # If all hard gates pass...
    return f"🟢 AUTHORIZED: {best_contract.ticker} {best_contract.strike} {best_contract.expiration}"
```

7. Summary Checklist for the Developer

    Do not rely solely on Yahoo Finance. It will cause your script to fail the VRP and IV Rank hard gates.
    Subscribe to a historical options data feed (Polygon.io is the most cost-effective for Python) to calculate IV Rank and Term Structure.
    Implement strict NULL checking. v3.5 explicitly states: If a required input cannot be verified: DATA INCOMPLETE — DO NOT AUTHORIZE.
    Use a local database (DuckDB/SQLite). Querying 100 symbols via API calls every time you want to run the plan will be too slow and hit rate limits. Download the data once a day, store it locally, and run the v3.5 logic against the local DB.
    Calculate Realized Volatility correctly. Use log returns and annualize by multiplying by sqrt(252), not sqrt(365).
    **Do not conflate IV Rank and IV Percentile.** Use the min/max formula for any field named `iv_rank`; if a percentile-style measure is wanted, name and document it separately as `iv_percentile`.
    **Use a single-payment dividend amount, not an annualized rate**, for the Stage 9/31 extrinsic-vs-dividend assignment-risk check.
    **Decide IV rescaling (`÷100`) at the chain level, not per-contract**, so genuinely high-IV individual contracts aren't silently corrupted.

## 8. Known Fixes Log

| Date | Issue | Fix |
| :--- | :--- | :--- |
| (this review) | `parsing.safe_float` used Python-2-only `except TypeError, ValueError:` syntax, which is a `SyntaxError` under Python 3 (confirmed against `pyproject.toml`'s `requires-python = ">=3.14"`) and prevents the module — and therefore the whole pipeline — from importing at all. | Changed to `except (TypeError, ValueError):`. |
| (this review) | Dividend amount used for Stage 9/31 assignment-risk was sourced from `dividendRate`/`trailingAnnualDividendRate` (annualized), overstating the relevant single-payment dividend by roughly the payment frequency (~4x for quarterly payers). | Added `get_next_dividend_amount()`, preferring the last actual payment from `ticker.dividends`, falling back to `annual_rate / 4` only when history is unavailable, with source/estimated flags stored and surfaced as a candidate warning. |
| (this review) | `get_iv_rank()` computed `mean(history < iv_today) * 100`, which is IV **Percentile**, not IV **Rank** — despite being stored in a field named `iv_rank` and gated against IV-Rank-style thresholds. Can diverge sharply from a brokerage's displayed IV Rank. | Changed the historical-IV branch to the standard min/max IV Rank formula: `(iv_today - hist.min()) / (hist.max() - hist.min()) * 100`. |
| (this review) | `fetch_options_chain()` rescaled any individual contract's IV (`÷100`) whenever it read `> 3.0`, which can silently corrupt legitimately high-IV deep-OTM/small-cap/event-driven contracts. | Rescale decision now made once per symbol at the chain level, based on whether a majority of that chain's valid IV prints exceed the threshold. |
