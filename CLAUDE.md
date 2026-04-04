# Claude Trading Bot — Development Log

This file documents every improvement made to the bot, plan by plan, in order.
Deployed on Heroku as a worker process. KuCoin (spot) + MEXC (futures).

---

## Architecture Overview

```
bot.py          — Main loop, exchange connections, pair selection, trade management
strategy.py     — All signal logic: indicators, regime detection, entry/exit rules
performance.py  — Database (PostgreSQL), trade saving, TP/SL monitoring, reports
logger.py       — Telegram alerts
backtest.py     — Historical validation engine (runs locally, not on Heroku)
```

---

## Plan 1 — Volume Floor Fix

**Problem:** Bot was scanning only the first 80 symbols from KuCoin by alphabetical/API order. During low-volume periods (e.g. April 2026 tariff selloff), most alts dropped below the $150K/h volume gate leaving only XMR/USDT.

**Changes: `bot.py`**
- Lowered 1h volume floor from `$150K` to `$75K`
- Expanded symbol scan from `[:80]` to `[:150]` spot, `[:120]` futures

---

## Plan 2 — Volume-Based Pair Discovery

**Problem:** Alphabetical scan meant BTC, ETH, SOL were excluded or ranked unfairly. Position in the market dict determined what the bot saw, not actual liquidity.

**Changes: `bot.py`**

Replaced `get_pairs()` approach with a two-stage pipeline:

**Stage 1 — `_get_liquid_active_pool()`**
- Single `fetch_tickers()` API call — gets 24h data for ALL pairs simultaneously
- Filters: stablecoin blocklist + `$2M` 24h volume gate + `|1.5%|` movement gate
- Sorts survivors by 24h volume descending — BTC/ETH/SOL rank naturally at top
- Returns top 50 per exchange

**Stage 2 — `momentum_score()`**
- Scores each of the 50 by `ATR% × 3h_avg_volume × surge_multiplier`
- Surge multiplier: if last 1h volume > 20-candle average → coin is heating up NOW (capped 3×)
- Returns top 20 for strategy evaluation

---

## Plan 3 — Stablecoin + Non-Crypto Blocklist

**Problem:** USDC/USDT, DAI/USDT, XAUT/USDT (gold), USOIL, UKOIL, SILVER perpetuals appearing in the pair pool, wasting fetch calls and polluting the scan.

**Changes: `bot.py`**

Added two blocklists applied at the ticker stage:

```python
_STABLES = {USDC, DAI, BUSD, TUSD, FDUSD, FRAX, USDP, UST, USDD, SUSD,
            GUSD, LUSD, PYUSD, USDJ, CUSD, CEUR, EURS, ALUSD, USDN, MUSD, USDX}

_NON_CRYPTO = {XAUT, PAXG, CACHE, XAU, XAG, XAGT,     # gold/silver tokens
               SILVER, GOLD,                             # metal perpetuals
               USOIL, UKOIL, OIL, BRENT, WTI,           # oil perpetuals
               WHEAT, CORN, SOYB}                        # agricultural
```

---

## Plan 4 — Strategy Upgrades for Mid-Cap Explosive Moves

**Goal:** Increase RR and signal quality. Focus on coins that coil then break out violently.

### 4a — BB Squeeze Detection (`strategy.py`)

New function `is_bb_squeeze(df)`:
- Detects when at least 4 of the last 5 candles had Bollinger Band width below 85% of its 50-candle average (compression)
- AND current candle shows BBW expanding (breakout beginning)
- Applied as a hard gate on trend entries

### 4b — Consolidation Coil (`strategy.py`)

New function `consolidation_coil(df, atr)`:
- Checks that at least 4 consecutive candles before the current one had range `< 0.6 × ATR`
- Confirms the tight coil before the breakout
- Applied alongside BB squeeze on trend entries

### 4c — Entry Trigger Change (`strategy.py`)

Replaced EMA9/21 crossover check with breakout trigger:
- **BUY**: `last['close'] > prev['high']` — current 15m candle closes above previous high
- **SELL**: `last['close'] < prev['low']` — current 15m candle closes below previous low

Faster, more explosive entry timing. Confirms momentum rather than lagging EMA alignment.

### 4d — Tiered Take Profits (`strategy.py`, `performance.py`, `bot.py`)

Added TP1 + TP2 system:
- **TP1** = nearest swing high/low on 1h (existing logic) → alert: "Close 50%"
- **TP2** = second swing high/low beyond TP1 → alert: "Close 25%, trail rest"
- If no TP2 exists, full position exits at TP1

New functions in `strategy.py`:
- `second_resistance(df_1h, tp1)` — next swing high above TP1
- `second_support(df_1h, tp1)` — next swing low below TP1

DB changes in `performance.py`:
- Added `tp2 FLOAT` and `tp1_hit BOOLEAN` columns to trades table
- `ALTER TABLE IF NOT EXISTS` used for safe migration on existing live DB
- `check_trade_results()` updated with full tiered logic

Signal return value: now 8-tuple `(direction, entry, sl, tp1, tp2, rr, atr, trade_type)`

### 4e — RR Minimum Raised

- Old: `rr < 2.0` → reject
- New: `rr < 2.5` → reject
- Only the cleanest setups with genuine structural room qualify

### 4f — Mid-Cap Price Filter (`bot.py`)

```python
MID_CAP_MIN = 0.10   # excludes sub-cent noise
MID_CAP_MAX = 150.0  # excludes BTC (~$80K) and ETH (~$2K)
```

Applied in `run_bot()` before indicators run. Focuses the universe on mid-caps with the highest explosive potential.

---

## Plan 5 — Adaptive Strategy Parameters

**Problem:** Fixed ADX threshold (22), StochRSI limits (72/28), and RR minimum (2.5) treat all market conditions identically. High-volatility regimes need looser filters (more signals, bigger moves). Low-volatility regimes need tighter filters (only the cleanest setups).

**Changes: `strategy.py`**

New function `get_regime_params(df_4h)`:
- Computes current ATR percentile rank against the pair's own 4h ATR history
- Returns adaptive thresholds:

| Regime | ATR rank | ADX min | StochRSI OB/OS | Min RR |
|---|---|---|---|---|
| HIGH volatility | > 70th pct | 18 | 78 / 22 | 2.0 |
| NORMAL | 30–70th pct | 22 | 72 / 28 | 2.5 |
| LOW volatility | < 30th pct | 25 | 68 / 32 | 3.0 |

Computed once in `generate_filtered_signal()` and threaded into:
- `is_trending()` — adaptive ADX gate
- `detect_htf_reversal()` — adaptive StochRSI extremes
- `entry_signal_trend()` — adaptive StochRSI + RR minimum
- `entry_signal_reversal()` — adaptive StochRSI + RR minimum

---

## Plan 6 — MEXC Futures fetch_tickers Fix

**Problem:** Three layered bugs caused `fetch_tickers failed (futures): empty pool` on every scan:

**Bug 1 — Wrong exchange type**
`futures_exchange = ccxt.mexc(...)` had no `defaultType` set. MEXC defaulted to spot when `fetch_tickers()` was called, returning symbols like `BTC/USDT`. The futures filter `"/USDT:USDT" in s` rejected every single one.

Fix:
```python
futures_exchange.options['defaultType'] = 'swap'
```

**Bug 2 — quoteVolume is None on MEXC futures**
MEXC futures tickers return `quoteVolume = None`. The old code did `None or 0 = 0` → failed `$2M` gate → empty pool.

Fix: fallback to `baseVolume × last_price`:
```python
vol_24h = t.get("quoteVolume") or 0
if vol_24h == 0:
    vol_24h = (t.get("last") or 0) * (t.get("baseVolume") or 0)
```

**Bug 3 — percentage is None on MEXC futures**
`abs(None or 0) = 0` → always failed the 1.5% movement gate.

Fix: only apply movement filter when data is actually available:
```python
pct_raw = t.get("percentage")
if pct_raw is not None and abs(pct_raw) < 1.5:
    continue
```

**Also fixed:** error logging split into two separate blocks so fetch errors and empty-pool errors print with the actual exception type, not the same generic message.

---

## Plan 7 — Backtest Engine

**File:** `backtest.py` (runs locally, not on Heroku)

**Design principles:**
- Imports `apply_indicators` and `generate_filtered_signal` directly from `strategy.py` — zero duplication, auto-reflects any strategy change
- Walk-forward simulation: at each 15m candle `i`, slices all dataframes to `df.iloc[:i+1]` — zero lookahead bias
- Same trade management logic as `performance.py`: breakeven at 1:1, trail tightens at 2:1, TP1 partial, TP2 runner
- Same mid-cap price filter as `bot.py`
- Adaptive params run automatically inside `generate_filtered_signal`

**How to run:**
```bash
cd /home/entitypak/claude/claude-trading-bot

# Default: 8 pairs, 90 days, KuCoin spot
python backtest.py

# Custom
python backtest.py --days 180 --symbols SOL/USDT AAVE/USDT LINK/USDT ATOM/USDT

# MEXC futures
python backtest.py --futures --days 90
```

**Report output:** Win rate, total R, expectancy, profit factor, max drawdown, Sharpe ratio, per-symbol breakdown, best/worst trade, edge grade (A/B/C/F). Full trade log saved as CSV.

**Minimum for reliable stats:** 30+ trades. Run `--days 180` or add more symbols if count is low.

---

## Current Signal Flow (End to End)

```
Every 15 minutes:

1. fetch_tickers() [one API call per exchange]
   → block stablecoins + non-crypto commodities
   → gate: $2M 24h volume (baseVolume×price fallback for MEXC)
   → gate: |1.5%| movement (skipped if data unavailable)
   → sort by 24h volume
   → top 50 per exchange

2. momentum_score() for each of the 50
   → score = ATR% × 3h_vol × surge_mult
   → gate: $75K/h 1h volume
   → top 20 proceed

3. For each of the 20 pairs:
   → mid-cap filter: $0.10 – $150 price
   → fetch 15m / 1h / 4h / 1d candles
   → apply_indicators() on all 4 timeframes

4. generate_filtered_signal():
   → get_regime_params(df_4h) — detect HIGH/NORMAL/LOW volatility
   → is_trending(df_4h, adx_min) — adaptive ADX gate
   → detect_htf_reversal() — 4 conditions: structure divergence +
     extreme StochRSI + volume surge + MACD flip
   → get_htf_bias() — 4/5 confluence points required
   → entry_signal_trend() or entry_signal_reversal():
       - close > prev_high (BUY) / close < prev_low (SELL)
       - BB squeeze + consolidation coil (trend only)
       - StochRSI not overbought/oversold (adaptive)
       - 1h MACD confirmation (trend only)
       - volume > 1.15× vol_ma
       - structural SL (swing low/high ± 0.3 ATR)
       - TP1 = nearest 1h swing level
       - TP2 = second 1h swing level (runner)
       - RR ≥ adaptive minimum (2.0 / 2.5 / 3.0)

5. Signal fires → pending queue (waits for entry to be touched)
6. Entry hit → live trade saved to DB
7. Trade monitored every 15 min:
   → 1:1 hit → SL to breakeven
   → 2:1 hit → trail tightens (price - 1.2×risk)
   → TP1 hit → alert "Close 50%", tp1_hit=True
   → TP2 hit → alert "Close 25%, trail rest", status=WIN
   → trail_sl hit → status=BE_WIN or LOSS
```

---

## Database Schema (PostgreSQL)

**trades table:**
```
time, pair, signal, entry, sl, tp, tp2, rr, status,
market_type, atr, be_activated, trail_sl, tp1_hit
```

**pending_trades table:**
```
pair, signal, entry, sl, tp, tp2, rr, market_type,
trade_type, atr, queued_at
```

Status values: `OPEN`, `WIN`, `BE_WIN`, `LOSS`

---

## Telegram Commands

| Command | Action |
|---|---|
| `/status` | Open + pending trades with entry/RR |
| `/stats` | All-time win rate, expectancy, streak |
| `/cancel SYMBOL` | Remove a pending signal |
| `/help` | Command list |

---

## Environment Variables

| Var | Default | Purpose |
|---|---|---|
| `ACCOUNT_BALANCE` | 15 | Account size for position sizing |
| `RISK_PCT` | 0.02 | Risk per trade (2%) |
| `DATABASE_URL` | — | PostgreSQL connection string |
| `TOKEN` | — | Telegram bot token |
| `CHAT_ID` | — | Telegram chat ID |

---

## Bot Rating History

| Version | Grade | Key addition |
|---|---|---|
| Original | 7.5/10 | Base strategy |
| After Plan 2–3 | 8.0/10 | Volume-based discovery, stablecoin filter |
| After Plan 4–5 | 8.8/10 | BB squeeze, tiered TP, adaptive params, mid-cap focus |
| After Plan 6–7 | 9.0/10 | Futures fixed, backtest engine |

**Gap to 10/10:** Live order execution (currently manual alerts), session filter, account balance auto-sync, minimum order value check.
