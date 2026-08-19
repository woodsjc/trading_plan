# plan.md: Data Collection & Pipeline Architecture for Options Trading Plan v3.3

## 1. The Data Source Reality Check
**Is there a single datasource like Yahoo Finance that has *everything* already available?**
**No.** There is no single free or low-cost retail API that provides every data point required by v3.3 out-of-the-box. 

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

## 4. Mapping v3.3 Stages to the Data Pipeline

Here is exactly how the collected data satisfies the hard gates of the v3.3 plan.

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

---

## 5. Database Schema Design (For 100 Symbols)

**Table: `underlying_metrics` (Updated Daily/Pre-market)**
| Column | Type | v3.3 Stage |
| :--- | :--- | :--- |
| `symbol` | VARCHAR (PK) | All |
| `last_updated` | TIMESTAMP | Stage 0 |
| `price` | FLOAT | Stage 1 |
| `beta` | FLOAT | Stage 10 |
| `market_cap` | BIGINT | Stage 1 |
| `earnings_date` | DATE | Stage 3 |
| `ex_div_date` | DATE | Stage 9 |
| `div_amount` | FLOAT | Stage 9 |
| `iv30` | FLOAT | Stage 2 |
| `iv_rank` | FLOAT | Stage 2 |
| `rv_20d` | FLOAT | Stage 2 |
| `rv_30d` | FLOAT | Stage 2 |
| `vrp_pp` | FLOAT | Stage 2 |
| `vrp_ratio` | FLOAT | Stage 2 |
| `rf_rate` | FLOAT | Stage 7 |

> **Collection scope:** Only fetch/store expirations in the **30–40 DTE** window. Expirations outside this range should be skipped at fetch time (not just filtered later), to reduce API calls and keep the table lean for a ~100-symbol universe.

**Table: `options_contracts` (Updated Daily/Pre-market)**
| Column | Type | v3.3 Stage |
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
| `delta` | FLOAT | Stage 4 |
| `iv` | FLOAT | Stage 4 |
| `open_interest` | INT | Stage 5 |
| `volume` | INT | Stage 5 |
| `extrinsic_value`| FLOAT | Stage 9 |

---

## 6. Execution & "Do Nothing" Logic

The Python script should output a daily "Actionable Report" using the exact v3.3 Decision Labels.

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
        (data.options.dte.between(30, 40)) &
        (data.options.oi >= 250) & 
        (data.options.volume >= 10) & 
        (data.options.spread_pct <= 0.10) &
        (data.options.delta.between(0.15, 0.30))
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
    Implement strict NULL checking. v3.3 explicitly states: If a required input cannot be verified: DATA INCOMPLETE — DO NOT AUTHORIZE.
    Use a local database (DuckDB/SQLite). Querying 100 symbols via API calls every time you want to run the plan will be too slow and hit rate limits. Download the data once a day, store it locally, and run the v3.3 logic against the local DB.
    Calculate Realized Volatility correctly. Use log returns and annualize by multiplying by sqrt(252), not sqrt(365).
