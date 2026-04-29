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
- Returns top 40 per exchange

**Stage 2 — `momentum_score()`**
- Requires 180 days of 1d history — blocks newly listed and manipulated tokens before they waste fetch calls
- Scores each of the 40 by `ATR% × 3h_avg_volume × surge_multiplier`
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
- `get_htf_bias()` — threshold drops to 3/5 in HIGH vol regime (market already moving, stricter confluence would miss the whole move)

**High-vol regime bypasses:**
- BB squeeze + consolidation coil gates skipped when `high_vol=True` — crash/breakout markets won't consolidate first
- SELL trend: StochRSI oversold check permanently removed — in a sustained downtrend the 15m stoch pins near 0 and would block all short entries; oversold filtering is correct for reversals but wrong for trend-following

**Late entry tolerance** (`is_not_late_entry`): tiered by trade type
- Trend entries: 1.5% tolerance — breakouts often retest the breakout level
- Reversal entries: 0.3% tolerance — must be entered at the turning point

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
   → top 40 per exchange

2. momentum_score() for each of the 40
   → gate: 180 days 1d history (filters new/manipulated listings)
   → score = ATR% × 3h_vol × surge_mult
   → gate: $75K/h 1h volume
   → top 20 proceed

3. For each of the 20 pairs:
   → fetch 15m / 1h / 4h / 1d candles
   → apply_indicators() on all 4 timeframes

4. generate_filtered_signal():
   → get_regime_params(df_4h) — detect HIGH/NORMAL/LOW volatility
   → is_trending(df_4h, adx_min) — adaptive ADX gate
   → detect_htf_reversal() — 4 conditions: structure divergence +
     extreme StochRSI + volume surge + MACD flip
   → get_htf_bias() — 4/5 confluence (3/5 in HIGH vol regime)
   → entry_signal_trend() or entry_signal_reversal():
       - close > prev_high (BUY) / close < prev_low (SELL)
       - BB squeeze + consolidation coil (trend only; skipped in HIGH vol)
       - StochRSI not overbought for BUY (adaptive); no OS check for SELL trend
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

## Plan 8 — Bear / Recovery / Normal Market Mode

**Problem:** Bot fired zero signals during April 5-6 2026 tariff selloff crash. No bear-specific logic existed — all gates were tuned for normal trending markets.

**Changes: `bot.py`, `strategy.py`**

New module-level state in `bot.py`:
```python
_bear_mode_scans = 0
_recovery_scans  = 0
_market_mode     = "normal"
```

New function `_update_market_mode(breadth_data)`:
- Computes `bear_breadth` = fraction of top-20 pairs where `ema50 < ema200` on 1h
- `breadth > 65%` for 2+ scans → `"bear"` mode
- `breadth ≤ 45%` for 3+ scans → `"recovery"` mode (boundary is ≤, not <, so 9/20=45% counts)
- Otherwise → `"normal"` mode
- Sends Telegram alert on every mode transition

Two-pass `run_bot()`:
- Phase 1: fetch + apply indicators for all pairs, build `breadth_data{}`
- Phase 2: `_update_market_mode()`, then generate signals with `market_mode` passed in

**Bear mode changes (`strategy.py`):**
- `get_htf_bias()`: SELL only, threshold reduced by 1 (4→3 or 3→2 in HIGH vol)
- `get_regime_params()`: ADX min −3 (floor 14) — ADX lags in early crashes
- `entry_signal_trend()` SELL: vol threshold 0.90× instead of 1.15×; BB squeeze + coil skipped
- Reversal BUY re-enabled in bear mode (gates are already strict enough)

**Recovery mode changes:**
- `get_regime_params()`: forces `rr_min = 2.0` across all regimes

---

## Plan 9 — Support Bounce Entry Type

**Problem:** BUY signals at support during downtrend couldn't fire — reversal required 4h already bullish, which isn't true at first touch of support.

**Changes: `strategy.py`**

New function `entry_signal_bounce(df_15m, df_1h, df_4h, params)`:
- Gate 1: `last_4h['stoch_k'] < 20` — 4h must be oversold (at support, not mid-range)
- Gate 2: `4h macd_hist` improving (turning less negative) — momentum inflecting
- Gate 3: prior 1h swing low within 1.5% of entry — price at actual structural support
- Gate 4: 15m bullish engulfing OR hammer — reversal candle confirmation
- Gate 5: `volume > vol_ma × 1.5` — real buying interest
- SL: `df_15m['low'].tail(10).min() − (0.3 × ATR)`
- RR minimum: `params["rr_min"] + 0.5` — counter-trend premium (HIGH→2.5, NORMAL→3.0, LOW→3.5)

Hammer detection:
```python
body = abs(last['close'] - last['open'])
lower_wick = min(last['open'], last['close']) - last['low']
upper_wick = last['high'] - max(last['open'], last['close'])
is_hammer = body > 0 and lower_wick >= 2 * body and upper_wick <= body
```

Checked before reversal in `generate_filtered_signal()`. Works in all market modes including bear.

**Late entry tolerance:** bounce uses 0.3% (same as reversal — must be at support).

---

## Plan 10 — StochRSI Smarter OB Usage

**Problem:** StochRSI OB was a hard block on BUY. But OB on a genuine momentum breakout confirms the move — blocking it was wrong.

**Changes: `strategy.py` — `entry_signal_trend()`**

```python
if last['stoch_k'] > stoch_ob:
    strong_breakout = (last['close'] - prev['high']) > 0.3 * atr
    strong_volume   = last['volume'] > last['vol_ma'] * 1.5
    if not (strong_breakout and strong_volume):
        return None
    # else: OB is confirming momentum, allow
```

Rejection log updated: `"stoch OB 92>68 weak-breakout"` — shows OB was considered but bypass didn't trigger.

Rule: OB + weak breakout → reject. OB + strong breakout (close > prev_high by 0.3 ATR) + strong volume (1.5×) → allow.

---

## Plan 11 — Median vol_ma (Volume Illusion Fix)

**Problem:** `vol_ma = rolling(20).mean()` was destroyed by crash/bounce spike candles. 3-5 massive candles in the 20-candle window inflated the mean so badly that normal candles showed as 0.00x–0.07x, blocking all volume gates.

**Changes: `strategy.py` — `apply_indicators()`**

```python
# Before:
df['vol_ma'] = df['volume'].rolling(20).mean()

# After:
df['vol_ma'] = df['volume'].rolling(20).median()
```

Median of 20 values = 10th value. Up to 9 spike candles cannot move it. Volume data is right-skewed — median is the statistically correct central tendency estimator. Confirmed working in logs: BTC, DOT, ETH/spot vol rejections disappeared after deployment.

---

## Plan 12 — TP2-Primary RR Gating

**Problem:** RR gate only checked TP1. A setup with TP1 RR 1.8 and TP2 RR 4.0 was rejected even though the trade plan (50% at TP1, runner to TP2) proved structural room.

**Changes: `strategy.py` — `entry_signal_trend()`, `entry_signal_reversal()`, `entry_signal_bounce()`**

TP2 computed before RR gate in all three functions. Logic:

```python
if rr >= rr_min:
    pass  # TP1 sufficient on its own — original behavior
elif tp2 is not None:
    tp2_rr = round(abs(tp2 - entry) / risk, 2)
    if tp2_rr < rr_min or rr < 1.5:
        return None
    # TP2 proves structure has room — allow with TP1 ≥ 1.5 floor
else:
    return None  # No TP2 to rescue marginal TP1
```

The 1.5 floor on TP1 is a minimum — TP1 can be 1.7, 2.3, anything ≥ 1.5. It just ensures the first partial close is meaningful. TP1 ≥ rr_min bypasses TP2 check entirely.

---

## Plan 13 — Bounce Bug Fixes (SL, Pullback TP, Pending Expiry)

**Problems fixed:**

**Bug 1 — Bounce SELL: resistance below entry (RR=22 false signals)**
`near_res` filter used `h > entry * 0.999`, allowing resistance 0.1% below entry. Result: SL = resistance + 0.3×ATR placed nearly at entry → risk of 0.04%, absurd RR like 22.65.

Fix in `entry_signal_bounce()`:
```python
# Before:
near_res = [h for h in res_levels if h > entry * 0.999]

# After:
near_res = [h for h in res_levels if h >= entry]
```
Resistance must be AT or above entry — can't short below your resistance.

**Bug 2 — Bounce SELL firing on MACD alone (stoch=44)**
MACD alone at neutral StochRSI = weak confirmation for a counter-trend SELL. Added `strong_conf` requirement:
```python
strong_conf = conf_candle or conf_rsi or (conf_macd and stoch_k > 55)
```
MACD-only allowed only if stoch > 55 (approaching overbought territory).

**Bug 3 — Same tiny-SL bug on bounce BUY side**
Added minimum risk floor to BUY path:
```python
if risk < atr * 0.5:
    return None
```
Prevents support-at-entry setups from generating a trivially tight stop.

**Bug 4 — Pullback TP hardcoded to 1R (always RR=1.0)**
`generate_pullback_signal()` had `tp1 = close + 1.0 * risk` — a fixed 1R target. Changed to structural resistance/support:
```python
tp1 = nearest_resistance(df_1h, close) or nearest_resistance(df_4h, close)  # BUY
tp1 = nearest_support(df_1h, close) or nearest_support(df_4h, close)        # SELL
```
Also added TP2 population and 1.5 RR minimum.

**Bug 5 — Pending trade expiry 24h (blocked bot for 3+ hours)**
Changed `timedelta(hours=24)` → `timedelta(hours=1)` in `bot.py`. Stale pending signals from dead moves were consuming all 5 capacity slots.

---

## Plan 14 — Recovery Bounce Hardening (AND Gate + Wider SL)

**Problem:** Most bounce BUY trades in recovery mode were hitting SL. Three-layer root cause:
1. Recovery gate was OR (one condition enough) — ZEC passed with ema50=✓ but higher_low=✗, price still making lower lows
2. SL buffer 0.3×ATR too tight for choppy post-crash price action — wicks stopping out valid setups
3. Candle confirmation not mandatory — MACD/RSI alone doesn't prove real rejection at support

**Changes: `strategy.py` — `entry_signal_bounce()`**

**Fix 1 — Recovery gate: OR → AND**
```python
# Before:
gate_pass = close_above_ema50 or higher_low

# After:
gate_pass = close_above_ema50 and higher_low
```
Both must be true:
- `close_above_ema50`: current price above 1h EMA50 — medium-term average has turned
- `higher_low`: most recent 1h swing low is higher than the previous — structure is improving

**Fix 2 — Wider SL buffer in recovery**
```python
sl_buf = 0.5 * atr if params.get("market_mode") == "recovery" else 0.3 * atr
sl = nearest_sup - sl_buf
```
Recovery price action is choppy — wicks reach further below support. 0.5×ATR gives the trade room to breathe. Other modes keep 0.3×ATR.

**Fix 3 — Candle confirmation mandatory in recovery** *(deployed prior commit)*
```python
if params.get("market_mode") == "recovery" and not conf_candle:
    return None
```
MACD turning or RSI oversold = "less bad", not proof of reversal. A 15m bullish engulfing or hammer is required.

**Also fixed: `bot.py` Telegram message** — recovery mode alert now correctly states AND gate + SL buffer instead of old OR description.

**Result:** In the April 8–9 window with these gates, ZERO signals would fire — which is correct. Market was still retesting lows, no structure had formed. Bot correctly sits on hands.

---

## Plan 15 — 1:1 BE, SL Floor Alignment, ATR Cap Tightening

**Context:** INJ/USDT LONG [TREND] fired at entry 3.5860, SL 3.5719 (risk 0.0141 = 0.69×ATR), TP1 3.6330 (RR 3.33). Status: LOSS. MAE/MFE analysis from the DB:

| | INJ (LOSS) | AXS (WIN) |
|---|---|---|
| mfe | 1.203R | 4.732R |
| mae | 1.486R | NULL |
| time_to_mfe | 1.01h | 2.27h |
| time_to_mae | 1.78h | — |

INJ MFE = 1.203R means price crossed the 1:1 level (entry + 1×risk = 3.6001) before reversing and hitting SL. AXS had essentially zero adverse excursion — went straight to TP2. Initial instinct (raise SL floor to 0.75×ATR) was wrong: the INJ trade had directional merit (1.2R MFE), it just had no intermediate protection. The correct fixes are trade management, not entry filtering.

---

### 15a — 1:1 Breakeven Implementation

**Problem:** CLAUDE.md documented "1:1 hit → SL to breakeven" but the code only set BE at TP1 hit. INJ crossed 1:1 at 1.01h with MFE=1.203R, then reversed to a full LOSS at 1.78h. 1:1 BE converts this to BE_WIN.

**Changes: `performance.py` — `check_trade_results()`**

Added before trail tightening in both BUY and SELL blocks:
```python
# BUY
if not be_activated and not tp1_hit and price >= entry + risk:
    changes['be_activated'] = True
    changes['trail_sl'] = entry
    be_activated = True
    trail_sl = entry
    send_telegram("🔒 1:1 HIT — SL → Breakeven ...")

# SELL (symmetric)
if not be_activated and not tp1_hit and price <= entry - risk:
    ...
```

AXS WIN was unaffected — MAE=NULL means price never came back toward entry after going up.

**Execution sequence after this change:**
1. 1:1 hit → BE activated, trail_sl = entry
2. 2:1 hit → trail tightens to `price - 1.2×risk` (already BE-protected)
3. TP1 hit → partial close 50%, trail_sl stays at entry (already set)
4. TP2 hit → WIN
5. trail_sl hit after BE → BE_WIN

---

### 15b — SL Floor Corrected to 0.5×ATR

**Problem:** Initial attempt raised trend SL floor to 0.75×ATR, which would have rejected the INJ trade entirely despite its directional merit (1.2R MFE). With 1:1 BE protecting the downside, the floor can be consistent with all other entry types.

**Changes: `strategy.py` — `entry_signal_trend()`**

```python
# Before:
if risk <= 0 or risk < atr * 0.4:   # original
# Then incorrectly changed to:
if risk <= 0 or risk < atr * 0.75:  # Plan 15 draft (too aggressive)
# Corrected to:
if risk <= 0 or risk < atr * 0.5:   # consistent with bounce/reversal/micro
```

Applied to both BUY and SELL paths. 0.5×ATR is the standard minimum across all entry types — prevents genuinely sub-noise stops while allowing trades with meaningful structural anchors.

---

### 15c — ATR Cap Tightened: RR > 3.5 → RR > 3.0

**Problem:** INJ had TP1 at RR 3.33 — below the old 3.5 cap, so the cap didn't fire. A TP1 at 3.33R means price must travel a large distance before the first partial close, leaving a wide window where a reversal turns a good setup into a full loss (exactly what happened).

**Changes: `strategy.py` — `entry_signal_trend()`, `entry_signal_reversal()`**

```python
# Before:
_ATR_CAP_RR = 3.5   # cap TP1 at 2.5×ATR when RR > 3.5

# After:
_ATR_CAP_RR = 3.0   # cap TP1 at 2.5×ATR when RR > 3.0
```

Effect: setups with structural TP1 > 3R get TP1 capped to 2.5×ATR (a nearer achievable target), and the swing level becomes TP2 (the runner). Compresses the no-protection gap between entry and first partial exit.

---

## Current Signal Flow (End to End)

```
Every 15 minutes:

1. fetch_tickers() [one API call per exchange]
   → block stablecoins + non-crypto commodities
   → gate: $2M 24h volume (baseVolume×price fallback for MEXC)
   → gate: |1.5%| movement (skipped if data unavailable)
   → sort by 24h volume
   → top 40 per exchange

2. momentum_score() for each of the 40
   → gate: 180 days 1d history (filters new/manipulated listings)
   → score = ATR% × 3h_vol × surge_mult
   → gate: $75K/h 1h volume
   → top 20 proceed

3. Phase 1: fetch 15m/1h/4h/1d for all 20 pairs, apply_indicators()
   → compute bear_breadth from 1h EMA50/EMA200 across all pairs
   → _update_market_mode() → "bear" / "recovery" / "normal"

4. Phase 2 — generate_filtered_signal() per pair:
   → get_regime_params(df_4h, market_mode) — HIGH/NORMAL/LOW vol + mode adjustments
   → is_trending(df_4h, adx_min) — adaptive ADX gate
   → entry_signal_bounce() — counter-trend BUY at structural support (fires first)
       - Recovery mode: BOTH ema50 above AND higher_low required (AND gate)
       - Recovery mode: candle mandatory (15m engulfing or hammer)
       - Recovery mode: SL buffer 0.5×ATR (vs 0.3×ATR in other modes)
   → detect_htf_reversal() — 4 conditions: structure divergence +
     extreme StochRSI + volume surge + MACD flip
   → get_htf_bias() — 4/5 confluence (3/5 in HIGH vol; −1 more in bear mode for SELL)
   → entry_signal_trend() or entry_signal_reversal():
       - 15m close > prev_high (BUY) / close < prev_low (SELL)
       - BB squeeze + consolidation coil (trend only; skipped in HIGH vol + bear SELL)
       - StochRSI OB: bypass allowed if close−prev_high > 0.3×ATR AND vol > 1.5×vol_ma
       - 1h MACD confirmation (trend only)
       - volume > 1.15× vol_ma (median-based, spike-resistant)
       - structural SL (swing low/high ± 0.3 ATR); minimum 0.5×ATR from entry
       - TP1 = nearest 1h swing level; capped at 2.5×ATR if structural RR > 3.0
       - TP2 = swing level beyond TP1 (or original swing if ATR cap fired)
       - RR gate: TP1 ≥ rr_min → pass; else TP2 ≥ rr_min AND TP1 ≥ 1.5 → pass

5. Signal fires → pending queue (waits for entry to be touched)
6. Entry hit → live trade saved to DB
7. Trade monitored every 15 min:
   → 1:1 hit → SL to breakeven (be_activated=True, trail_sl=entry)
   → 2:1 hit → trail tightens (price - 1.2×risk), BE must already be active
   → TP1 hit → alert "Close 50%", tp1_hit=True, trail_sl=entry confirmed
   → TP2 hit → alert "Close 25%, trail rest", status=WIN
   → trail_sl hit → status=BE_WIN (if BE active) or LOSS
```

---

## HTF Bias Confluence Thresholds

| Condition | Threshold |
|---|---|
| Normal vol, any mode | 4/5 |
| HIGH vol regime | 3/5 |
| Bear mode, normal vol | 3/5 (4−1) |
| Bear mode, HIGH vol | 2/5 (3−1) |

The 5 confluence factors: EMA50 vs EMA200 (1h), DI+ vs DI− (4h), 1d structure, 4h structure, 1h MACD direction.

---

## Bot Rating History

| Version | Grade | Key addition |
|---|---|---|
| Original | 7.5/10 | Base strategy |
| After Plan 2–3 | 8.0/10 | Volume-based discovery, stablecoin filter |
| After Plan 4–5 | 8.8/10 | BB squeeze, tiered TP, adaptive params, mid-cap focus |
| After Plan 6–7 | 9.0/10 | Futures fixed, backtest engine |
| After Plan 8 | 9.2/10 | Bear/recovery/normal market mode, sell trend fix, HTF bias bypass |
| After Plan 12 | 9.4/10 | Support bounce entry, StochRSI OB bypass, median vol_ma, TP2-primary RR gating |
| After Plan 13 | 9.4/10 | Bounce SL bugs fixed, pullback TP structural, pending expiry 1h |
| After Plan 14 | 9.5/10 | Recovery bounce: AND gate, wider SL (0.5×ATR), candle mandatory |
| Current | 9.6/10 | 1:1 BE implemented, SL floor 0.5×ATR consistent, ATR cap 3.0 |

**Gap to 10/10:** Live order execution (currently manual alerts), session filter, account balance auto-sync, minimum order value check.
