# Claude Trading Bot — Development Log

Railway worker process. KuCoin (spot) + MEXC (futures).
Current plan: **26** | Rating: **9.9+/10** | Mode: **PAPER TRADING ($100)**

---

## Architecture

```
bot.py               — Lightweight main loop (~33 MB idle). No ccxt/pandas/numpy.
                        Spawns screener_worker.py each scan cycle via subprocess.
screener_worker.py   — Isolated scan subprocess. Loads numpy+pandas+strategy,
                        runs the full signal scan, writes JSON to tmp file, exits.
                        OS reclaims all memory on subprocess exit.
db.py                — Hot-path DB layer (psycopg2 only, no pandas). Used every cycle
                        for trade CRUD, pending trades, guards.
performance.py       — Stats-only (pandas). Lazy-imported once daily for daily_report
                        and /stats. Never loaded at startup.
strategy.py          — All signal logic: indicators, regime detection, entry/exit rules
logger.py            — Telegram alerts
backtest.py          — Walk-forward validation engine (runs locally, not on Railway)
```

### RAM Profile
- Main process idle: ~33 MB (no ccxt/pandas/numpy)
- Worker at import: ~73 MB | after scan: ~83 MB | freed fully on exit
- Billing: ~1,194 MB·h/day (was 3,792 MB·h — **−69%**)

### Running

```bash
cd claude-trading-bot
pip install -r requirements.txt
python bot.py
```

---

## Signal Flow (End to End)

```
Every scan cycle (session-gated):

SESSION GATE
  00–07 UTC: blocked — trade management only, 15-min sleep
  08–17 UTC: filtered — conf ≥ 65 required, 10-min sleep
  18–23 UTC: full — conf ≥ 55, 5-min sleep

1. get_pairs()  [screener_worker.py]
   → fetch_tickers(): block stablecoins + non-crypto commodities
   → $2M 24h volume gate (baseVolume×price fallback for MEXC futures)
   → |1.5%| movement gate (skipped if data unavailable)
   → sort by volume → top 50 KuCoin spot + top 50 MEXC futures
   → momentum_score(): 180d history gate, score = ATR%×3h_vol×surge_mult, $75K/h gate
   → top 30 combined

2. Stage A — breadth + BTC macro (pure Python EMA — zero DataFrames)
   → bear_breadth = fraction of pairs with 1h EMA50 < EMA200
   → market_mode:
       "bear"     — breadth > 65% for 2+ consecutive scans
       "recovery" — breadth ≤ 45% for 3+ consecutive scans
       "normal"   — otherwise
   → btc_downtrend = BTC 4h EMA50 < EMA200 × 0.985  (>1.5% separation required)

3. Stage B — signal scan (one pair at a time; _RUN_CACHE evicted per pair)
   → fetch 15m/1h/4h/1d → apply_indicators()
   → diagnostic log: ADX4h, RSI1h, EMA bull/bear

   A. generate_filtered_signal() — high-conviction entries
      → get_regime_params(df_4h, market_mode):
            HIGH vol (ATR >70th pct): ADX-3, StochOB/OS 85/15, RR 2.0
            NORMAL  (30–70th pct):   ADX base, StochOB/OS 72/28, RR 2.5
            LOW vol (<30th pct):     ADX+3, StochOB/OS 68/32, RR 3.0
            Bear/recovery mode:      RR forced 2.0 all regimes
            Bear mode:               ADX-2 additional
      → BTC macro gate: downtrend=True → block BUY trend + BUY reversal
      → Bear mode only: entry_signal_fade_resistance() — SELL at 4h EMA50 resistance
      → detect_htf_reversal() — 4 conditions: structure divergence +
        extreme StochRSI + volume surge + MACD flip
      → get_htf_bias() — DI mandatory + 4 scored factors
          (1h struct, 4h struct, 1d struct, 1h EMA50/200):
            BUY  normal vol: 3/4 | HIGH vol: 2/4 | recovery: 2/4
            SELL normal vol: 4/4 | HIGH vol: 3/4 | bear: −1 from baseline
      → entry_signal_trend() or entry_signal_reversal():
            - close > prev_high (BUY) / close < prev_low (SELL)
            - min breakout distance 0.3×ATR  ← fakeout filter
            - BB squeeze AND consolidation coil (trend only; skipped HIGH vol + bear SELL)
            - StochRSI hard gate 85: bypass if breakout >0.3×ATR AND vol >1.5×vol_ma
            - 1h MACD confirmation (trend only)
            - volume > 1.15× vol_ma  (0.90× bear SELL)
            - structural SL (swing ± 0.3×ATR); minimum 0.5×ATR from entry
            - TP1 = nearest 1h swing; capped at 2.0×ATR when structural RR > 2.0
            - TP2 = next swing beyond TP1 (if ATR cap fired: level beyond original target)
            - RR gate: TP1 ≥ rr_min → pass; else TP2 ≥ rr_min AND TP1 ≥ 1.2 → pass

   B. generate_pullback_signal() — fallback if A returns None
      → get_regime_params()
      → ADX < threshold → entry_signal_range()
      → ADX ≥ threshold:
            - trend_ok = 4h EMA50 slope directional
            - rsi_ok   = 1h RSI in pullback zone (40–48 BUY, 52–60 SELL)
            - Bear: SELL only; recovery: BUY only
            - trend_ok AND rsi_ok AND (rsi_cross OR conf_candle) → PULLBACK entry
            - trend_ok only → DISABLED (bounce: backtest −0.062R)
            - EMA flat → DISABLED (micro: RR minimum too low)

4. compute_confidence() → 0–100  (4 layers × 25 pts each)
   Macro:     market_mode + BTC alignment with signal direction
   Structure: DI gap + HTF factor count
   Entry:     ADX excess + volume ratio + gate count
   Setup:     RR excess + TP2 existence + SL/ATR ratio

   → discard if conf < 55  (all sessions)
   → discard if conf < 65  (08–17 UTC filtered session)

5. Trend signals: record entry_valid_above (prev_high for BUY) / entry_valid_below (prev_low for SELL)

6. Signal → pending queue
   Expiry: 1h (trend/reversal/pullback) | 3h (bounce)

7. check_pending_trades() every cycle:
   → breakout invalidation: BUY removed if price < breakout×0.997  (falling knife)
   → breakdown invalidation: SELL removed if price > breakdown×1.003
   → entry hit: price ≤ entry×1.003 (BUY) / price ≥ entry×0.997 (SELL)
   → late-entry guard: |price − entry| / entry ≤ 1.5%
   → entry triggered → save_trade() + Telegram alert

8. check_trade_results() every cycle:
   → 1:1 hit   → SL to breakeven (be_activated=True, trail_sl=entry)
   → 2:1 hit   → trail tightens to price − 1.2×risk
   → TP1 hit   → Telegram "Close 50%", tp1_hit=True
   → TP2 hit   → Telegram "Close 25%, trail rest", status=WIN
   → trail hit → status=BE_WIN (if BE active) or LOSS
```

---

## Database Schema (PostgreSQL)

**trades:**
```
time, pair, signal, entry, sl, tp, tp2, rr, status,
market_type, atr, be_activated, trail_sl, tp1_hit, confidence, plan
```

**pending_trades:**
```
pair, signal, entry, sl, tp, tp2, rr, market_type,
trade_type, atr, queued_at, confidence
```

Status values: `OPEN`, `WIN`, `BE_WIN`, `LOSS`

---

## Telegram Commands

| Command | Action |
|---|---|
| `/status` | Open + pending trades with entry/RR |
| `/stats` | Win rate, expectancy, per-plan and per-confidence breakdown |
| `/cancel SYMBOL` | Remove a pending signal |
| `/help` | Command list |

---

## Environment Variables

| Var | Default | Purpose |
|---|---|---|
| `ACCOUNT_BALANCE` | 15 | Account size for position sizing |
| `RISK_PCT` | 0.02 | Risk per trade (base, overridden by confidence) |
| `DATABASE_URL` | — | PostgreSQL connection string |
| `TOKEN` | — | Telegram bot token |
| `CHAT_ID` | — | Telegram chat ID |

---

## HTF Bias Confluence Thresholds

DI+/DI− (4h) is a **mandatory gate** — always required, not scored.
The 4 remaining factors scored: 1h structure, 4h structure, 1d structure, 1h EMA50 vs EMA200.

| Direction | Condition | Scored factors needed |
|---|---|---|
| BUY | Normal vol | DI bull + 3/4 |
| BUY | HIGH vol | DI bull + 2/4 |
| BUY | Recovery mode | DI bull + 2/4 |
| SELL | Normal vol | DI bear + 4/4 (all) |
| SELL | HIGH vol | DI bear + 3/4 |
| SELL | Bear mode | DI bear + 3/4 normal vol, 2/4 HIGH vol (−1 from baseline) |

SELL is one step harder — mixed regimes produce false shorts.
Recovery 2/4: structure is forming, not yet confirmed across all timeframes.
Bear −1: 1d structure lags in early crash phases.

---

## Key Design Decisions

Non-obvious choices and the reasons behind them. Read before changing thresholds.

**Subprocess isolation (Plans 21-22)**
Python cannot free imported library RAM. Subprocess exits return pages to OS.
ccxt (58 MB) replaced with direct REST HTTP — zero marginal cost since requests is already resident.
Main process stays at ~33 MB; worker's ~73 MB import cost is paid once per scan then freed.

**_RUN_CACHE per-pair eviction (Plan 25)**
`del df_x` only removes the local reference — `_RUN_CACHE[key]` still holds the DF.
Without `_RUN_CACHE.pop()` in the `finally` block, all 30×4 DataFrames accumulate during Stage B.
Fix: pop each pair's 4 TF entries in `finally` immediately after processing.

**Median vol_ma instead of mean (Plan 11)**
Crash/bounce spikes inflate `rolling(20).mean()` so badly that normal candles show 0.07× vol_ma,
blocking all volume gates. Median of 20 is immune to up to 9 spike candles — correct for right-skewed data.

**BTC macro gate 1.5% threshold (Plan 24)**
Hard block at any ema50<ema200 caused zero signals at 0.27% separation ($216 on $78K BTC — noise).
Now requires `ema50 < ema200 * 0.985`. True bear (2–10%+ below) still blocked.

**Recovery mode HTF 2/4 (Plan 24)**
3/4 is a fully-trending market requirement. In recovery, price rises but structure hasn't formed
consistent higher-highs across 1h/4h/1d yet. Pairs correctly score 2/4 (EMA + one TF). 3/4 was impossible.

**BB squeeze OR coil (Plan 26, changed from AND)**
0.3×ATR minimum breakout already validates real velocity — the compression gate just confirms
price paused before this move. Either BB squeeze OR coil is sufficient evidence of that pause.
AND was over-filtering strong-trend continuations where price pulls back but doesn't fully coil.
High-vol and strong_4h_trend (ADX>28) still bypass both entirely.

**0.3×ATR minimum breakout distance (Plan 23)**
Micro tick-above (close > prev_high by 1 tick) is ~80% fakeout. 0.3×ATR = real velocity.
Consistent with OB bypass threshold (same value). Also used in breakout invalidation check.

**TP1 capped at 2.0×ATR (Plan 23)**
Structural TP1 at 2.5–3R meant the trade consumed all momentum reaching 1:1 and BE activated —
then had 1.5–2R more to travel before any partial close. 48 BE wins from this pattern.
2.0×ATR cap = only 1 ATR remaining after 1:1 BE → achievable. Original swing becomes TP2.

**1:1 BE activation (Plan 15)**
Code documented "1:1 → breakeven" but originally only set BE at TP1. INJ case: MFE=1.2R,
price crossed 1:1 then reversed to full LOSS at 1.78h. BE at 1:1 converts this to BE_WIN.

**SL minimum 0.5×ATR (Plan 15)**
Below this = sub-noise stop. Standard floor across all entry types (trend, reversal, bounce).

**Confidence floor 55/65 (Plan 20)**
Low-confidence signals fired at 1% risk but hit SL at high rates — wrong.
Floor prevents them from being sent at all. Off-peak (08-17 UTC) raises to 65.

**Position sizing from confidence (Plan 18)**
conf/100 × 2% scales base risk. Stars 1–5 = 1%–3% base risk. RR bonus on top. Hard cap 5%.

**Bounce re-enabled in pullback path (Plan 26)**
The −0.062R backtest was from the pre-Plan-13 buggy version (SL placed below entry, no risk floor).
Current bounce has AND gate, mandatory candle in recovery, 0.5×ATR SL buffer, 2/3 confirmation.
`elif trend_ok: return entry_signal_bounce(df_15m, df_1h, df_4h, params)`

**Pending expiry 1h trend / 3h bounce (Plan 13)**
Was 24h — stale signals from dead moves consumed all capacity slots for hours.

---

## Bot Rating History

| Version | Grade | Key addition |
|---|---|---|
| Plans 1-3 | 8.0/10 | Volume-based discovery, stablecoin filter, $2M gate |
| Plans 4-5 | 8.8/10 | BB squeeze/coil, tiered TP, adaptive regime params |
| Plans 6-7 | 9.0/10 | MEXC futures fixed, backtest engine |
| Plans 8-14 | 9.5/10 | Bear/recovery/normal mode, bounce entry, bounce bug fixes |
| Plans 15-17 | 9.7/10 | 1:1 BE, ATR cap 3.0, BTC macro gate, session gate |
| Plans 18-20 | 9.9/10 | Confidence score + quality gate conf≥55, bounce/micro disabled |
| Plans 21-22 | 9.9/10 | Subprocess isolation (−73% idle RAM), ccxt→HTTP, psycopg2 (−69% billing) |
| Plan 23 | 9.9+/10 | Fakeout filter 0.3×ATR, TP1 cap 2.0×ATR, breakout-invalidation gate |
| Plan 24 | 9.9+/10 | BTC gate 1.5% threshold, recovery HTF 2/4 — zero-signal deadlock fixed |
| Plan 25 | 9.9+/10 | _RUN_CACHE per-pair eviction — peak worker RAM N×4 DFs → ~4 DFs |
| Plan 26 | 9.9+/10 | Paper trading mode ($100, MAX_CONCURRENT 5), stock filter, BB/coil OR, bounce re-enabled |

**Gap to 10/10:** Live order execution (currently manual alerts), account balance auto-sync, min order value check.

---

## Plan 23 — Fakeout Filter + Tighter TP1

**Problem:** W:20 BE:48 L:80 (148 trades). Two root causes:

1. **80 direct losses (54%)** — trend entries fire when `close > prev_high` by any margin. 1-tick poke = qualifies. These micro-breakouts ~80% fakeout and reverse immediately. Worse: pending entry (`price ≤ entry×1.003`) fires on the way DOWN when a breakout is failing — bot enters a falling knife.

2. **48 BE wins** — structural TP1 at 2.5–3R from entry. After 1:1 BE at `entry+risk`, TP1 is still 1.5–2R away. Trade consumed its momentum getting to 1:1, never reached TP1.

**Fix 1 — Minimum breakout distance 0.3×ATR** (`strategy.py — entry_signal_trend()`):
```python
if (last['close'] - prev['high']) < 0.3 * atr:
    return None   # BUY fakeout
if (prev['low'] - last['close']) < 0.3 * atr:
    return None   # SELL fakeout
```

**Fix 2 — TP1 capped at 2.0×ATR** (`strategy.py`):
```python
_ATR_TP_MULT = 2.0   # was 2.5
_ATR_CAP_RR  = 2.0   # was 3.0
```
When cap fires, TP2 = next swing BEYOND the original structural target (not the structural target itself).
TP2 rescue floor: `rr < 1.2` (was 1.5) — ATR-capped TP1 is intentionally tighter than rr_min.

**Fix 3 — Breakout invalidation gate** (`screener_worker.py`, `bot.py`):
Worker records `entry_valid_above` (prev_high for BUY) / `entry_valid_below` (prev_low for SELL).
`check_pending_trades()` removes BUY if `price < entry_valid_above × 0.997` — failed breakout.

---

## Plan 24 — Signal Unblocking

**Problem:** Zero signals for days. Balance $15.00 → $8.27.

**Root cause 1 — BTC gate blocked 100% of BUY at 0.27% separation**
`BTC 4h: EMA50=77963 EMA200=78179 (bearish sep=0.27%)` → hard block on all BUY signals.
NEAR (ADX 80.9), HYPE (ADX 76.7), WLD (ADX 71.4), LLYSTOCK (ADX 82.8) all blocked by $216 gap on $78K BTC.

**Fix** (`screener_worker.py — _check_btc_macro()`):
```python
downtrend = ema50 < ema200 * 0.985   # >1.5% below required (was: any gap)
```

**Root cause 2 — RECOVERY mode required 3/4 HTF, max achievable was 2/4**
Pairs scored: DI=bull (mandatory) + EMA50>200 (+1) + one bullish structure TF (+1) = 2/4.
Failed the 3/4 gate that requires a fully-trending market.

**Fix** (`strategy.py — get_htf_bias()`):
```python
if params and params.get("high_vol"):
    buy_threshold = 2
elif market_mode == "recovery":
    buy_threshold = 2   # structure forming, not confirmed
else:
    buy_threshold = 3
```

---

## Plan 25 — _RUN_CACHE Per-Pair Eviction

**Problem:** `del df_15m, df_1h, df_4h, df_1d` in Stage B `finally` block only removes local references.
`_RUN_CACHE[key]` still holds each DataFrame — all 30 pairs × 4 TFs accumulate simultaneously.
Peak worker RAM: N×4 DataFrames held at once instead of ~4.

**Fix** (`screener_worker.py — Stage B finally block`):
```python
finally:
    del df_15m, df_1h, df_4h, df_1d
    for _tf in ("15m", "1h", "4h", "1d"):
        _RUN_CACHE.pop(f"{symbol}_{_tf}_{market_type}", None)
```

Each pair's DataFrames are now eligible for GC immediately after processing.
`_RUN_CACHE.clear()` at end of Stage B retained as a safety net.
