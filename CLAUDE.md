# Claude Trading Bot — Development Log

Railway worker process. KuCoin (spot) + MEXC (futures).
Current plan: **28** | Rating: **9.9+/10** | Mode: **PAPER TRADING ($100)**

---

## Architecture

```
bot.py               — Lightweight main loop (~42 MB idle, permanent). No ccxt/pandas/numpy.
                        Spawns screener_worker.py each scan and report_worker.py once daily.
screener_worker.py   — Isolated scan subprocess. Loads numpy+pandas+strategy,
                        runs the full signal scan, writes JSON to tmp file, exits.
report_worker.py     — Isolated report subprocess. Loads pandas+sqlalchemy, runs
                        daily_report or get_stats_summary, sends via Telegram, exits.
db.py                — Hot-path DB layer (psycopg2 only, no pandas). Every cycle.
performance.py       — Stats-only (pandas). Only imported inside report_worker.py.
strategy.py          — All signal logic: indicators, regime detection, entry/exit rules
logger.py            — Telegram alerts
backtest.py          — Walk-forward validation engine (runs locally, not on Railway)
```

### RAM Profile
- Main: **~42 MB permanent** | Scan worker: ~83 MB peak (freed on exit) | Report worker: ~100 MB peak
- Billing: **~1,271 MB·h/day** (was 3,792 — **−66%** via subprocess isolation)

### Running
```bash
cd claude-trading-bot && pip install -r requirements.txt && python bot.py
```

---

## Signal Flow

```
SESSION GATE
  00–07 UTC: blocked (trade management only, 15-min sleep)
  08–17 UTC: filtered (conf ≥ 65, 10-min sleep)
  18–23 UTC: full (conf ≥ 55, 5-min sleep)

1. get_pairs() [screener_worker.py]
   → block stablecoins + non-crypto commodities
   → $2M 24h volume gate | |1.5%| movement gate
   → top 50 KuCoin spot + top 50 MEXC futures by volume
   → momentum_score(): 180d history gate, score = ATR%×3h_vol×surge_mult, $75K/h gate
   → top 30 combined

2. Stage A — breadth + BTC macro (pure Python EMA, zero DataFrames)
   → market_mode: "bear" (breadth>65% for 2+ scans) | "recovery" (≤45% for 3+) | "normal"
   → btc_downtrend = BTC 4h EMA50 < EMA200 × 0.985 (>1.5% sep required)

3. Stage B — per-pair signal scan (_RUN_CACHE evicted per pair in finally block)
   → fetch 15m/1h/4h/1d → apply_indicators()

   A. generate_filtered_signal() — high-conviction entries
      → get_regime_params(): HIGH vol → ADX-3/RR 2.0 | NORMAL → base/RR 2.5 | LOW → ADX+3/RR 3.0
        Bear/recovery: RR forced 2.0 | Bear: ADX-2 additional
      → BTC downtrend → block BUY trend + BUY reversal
      → get_htf_bias(): DI mandatory + 4 scored factors (1h/4h/1d struct, 1h EMA50/200)
      → entry_signal_trend() / entry_signal_reversal():
          - 0.3×ATR min breakout distance (fakeout filter)
          - BB squeeze OR coil (skipped HIGH vol + strong_4h_trend ADX>28)
          - StochRSI hard gate 85 (bypass if breakout >0.3×ATR AND vol >1.5×vol_ma)
          - 1h MACD confirmation (trend only) | vol > 1.15× vol_ma (0.90× bear SELL)
          - structural SL (swing ± 0.3×ATR); min 0.5×ATR from entry
          - TP1 = nearest 1h swing; capped at 2.0×ATR | TP2 = next swing beyond TP1
          - RR gate: TP1 ≥ rr_min → pass; else TP2 ≥ rr_min AND TP1 ≥ 1.2 → pass

   B. generate_pullback_signal() — fallback
      → ADX < threshold → entry_signal_range()
      → ADX ≥ threshold: trend_ok AND rsi_ok (40–48 BUY / 52–60 SELL) AND (cross OR candle)
        Bear: SELL only | recovery: BUY only
        trend_ok only → DISABLED (bounce) | EMA flat → DISABLED (micro)

4. compute_confidence() → 0–100 (4 layers × 25 pts)
   Macro: market_mode + BTC alignment | Structure: DI gap + HTF factors
   Entry: ADX excess + vol ratio + gate count | Setup: RR excess + TP2 + SL/ATR
   → discard conf < 55 (all) | conf < 65 (08–17 UTC)

5. Trend signals: record entry_valid_above/below (prev_high/low)

6. Signal → pending queue. Expiry: 1h (trend/reversal/pullback) | 3h (bounce)

7. check_pending_trades():
   → BUY removed if price < breakout×0.997 (falling knife)
   → SELL removed if price > breakdown×1.003
   → entry hit → save_trade() + Telegram alert (late-entry guard ≤1.5%)

8. check_trade_results():
   → 1:1 → SL to breakeven | 2:1 → trail to price−1.2×risk
   → TP1 hit → "Close 50%" | TP2 hit → "Close 25%, trail rest", status=WIN
   → trail hit → BE_WIN (if BE active) or LOSS
```

---

## Database Schema (PostgreSQL)

**trades:** `time, pair, signal, entry, sl, tp, tp2, rr, status, market_type, atr, be_activated, trail_sl, tp1_hit, confidence, plan`

**pending_trades:** `pair, signal, entry, sl, tp, tp2, rr, market_type, trade_type, atr, queued_at, confidence, signal_log_id`

**signal_log:** `id (PK), generated_at, pair, signal, trade_type, entry, sl, tp, tp2, rr, atr, confidence, market_type, session, market_mode, btc_downtrend, stage, price_at_gen, snap_4h, dir_4h, snap_24h, dir_24h, trade_time, entry_reached, tp1_reached, sl_reached, setup_outcome`
  - stage: `queued` | `filled` | `expired` | `invalidated` | `cancelled`
  - dir_4h/dir_24h: TRUE = price moved in predicted direction after 4h/24h
  - entry_reached/tp1_reached/sl_reached: computed retroactively via 1h OHLCV
  - setup_outcome: `win`|`loss`|`no_entry`|`ambiguous`|`open`|`executed`

Status: `OPEN`, `WIN`, `BE_WIN`, `LOSS`

---

## Telegram Commands

| Command | Action |
|---|---|
| `/status` | Open + pending trades with entry/RR |
| `/stats` | Win rate, expectancy, per-plan and per-confidence breakdown |
| `/cancel SYMBOL` | Remove a pending signal |
| `/edge` | Signal funnel + direction accuracy @ 4h (last 30d) |
| `/edge atr` | Funnel + direction accuracy split by ATR% environment (<2%, 2-5%, >5%) |
| `/edge dir` | Direction accuracy by BUY/SELL |
| `/edge session` | Breakdown by trading session |
| `/edge regime` | Breakdown by market regime |
| `/edge conf` | Breakdown by confidence band |
| `/edge type` | Breakdown by trade type |
| `/edge pair SYMBOL` | Pair-specific funnel + accuracy |
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

DI+/DI− (4h) = **mandatory gate** (not scored). 4 scored factors: 1h struct, 4h struct, 1d struct, 1h EMA50/200.

| Direction | Condition | Scored factors needed |
|---|---|---|
| BUY | Normal vol | DI bull + 3/4 |
| BUY | HIGH vol / Recovery | DI bull + 2/4 |
| SELL | Normal vol | DI bear + 4/4 |
| SELL | HIGH vol | DI bear + 3/4 |
| SELL | Bear mode | −1 from baseline (3/4 normal, 2/4 HIGH vol) |

SELL harder: mixed regimes produce false shorts. Recovery 2/4: structure forming, not confirmed. Bear −1: 1d lags in early crash.

---

## Key Design Decisions

Read before changing thresholds. These are non-obvious choices with specific reasons.

**Subprocess isolation (Plans 21-22)**
Python cannot free imported library RAM. Subprocess exits return pages to OS.
ccxt (58 MB) → direct REST HTTP. Main stays ~42 MB; worker ~83 MB is freed per scan.

**_RUN_CACHE per-pair eviction (Plan 25)**
`del df_x` only removes the local reference — `_RUN_CACHE[key]` still holds the DF.
Pop each pair's 4 TF entries in `finally` immediately after processing; `.clear()` at end as safety net.

**Median vol_ma instead of mean (Plan 11)**
Crash spikes inflate `rolling(20).mean()` → normal candles show 0.07× vol_ma, blocking all gates.
Median of 20 is immune to up to 9 spike candles — correct for right-skewed volume data.

**BTC macro gate 1.5% threshold (Plan 24)**
Hard block at any ema50<ema200 caused zero signals at 0.27% separation ($216 noise on $78K BTC).
`ema50 < ema200 * 0.985` — true bear (2–10%+ below) still blocked.

**Recovery mode HTF 2/4 (Plan 24)**
3/4 requires a fully-trending market. In recovery, pairs score DI bull + EMA + one TF = 2/4 max.
3/4 was structurally impossible — zero-signal deadlock.

**BB squeeze OR coil (Plan 26, changed from AND)**
0.3×ATR breakout already validates velocity. OR is sufficient — AND over-filtered strong continuations.
High-vol and ADX>28 bypass both entirely.

**0.3×ATR minimum breakout distance (Plan 23)**
1-tick close above prev_high is ~80% fakeout. Same threshold as OB bypass and invalidation check.

**TP1 capped at 2.0×ATR (Plan 23)**
Structural TP1 at 2.5–3R → 48 BE wins (trade exhausted momentum before TP1). 2.0×ATR cap → 1 ATR left after 1:1 BE. Original swing becomes TP2; TP2 rescue floor `rr < 1.2`.

**1:1 BE activation (Plan 15)**
Originally BE was set only at TP1. INJ case: MFE=1.2R, reversed to full LOSS before TP1. BE at 1:1 = BE_WIN.

**SL minimum 0.5×ATR (Plan 15)**
Below this = sub-noise stop. Floor across all entry types.

**Confidence floor 55/65 (Plan 20)**
Low-conf signals fired at 1% risk but hit SL at high rates. Floor suppresses them entirely.
Off-peak (08–17 UTC) raises to 65.

**Position sizing from confidence (Plan 18)**
`conf/100 × 2%` scales base risk. Stars 1–5 = 1–3% base risk + RR bonus. Hard cap 5%.

**Bounce re-enabled in pullback path (Plan 26)**
−0.062R backtest was pre-Plan-13 bug (SL below entry, no floor). Current bounce has AND gate,
mandatory candle in recovery, 0.5×ATR buffer, 2/3 confirmation.

**Pending expiry 1h trend / 3h bounce (Plan 13)**
Was 24h — stale signals from dead moves consumed capacity slots.

---

## Plan History

Plans 1–22: volume gates, subprocess isolation, bear/recovery mode, confidence score, psycopg2.
Plan 23: fakeout filter (0.3×ATR), TP1 cap (2.0×ATR), breakout-invalidation gate.
Plan 24: BTC gate 1.5% threshold, recovery HTF 2/4 — zero-signal deadlock fixed.
Plan 25: _RUN_CACHE per-pair eviction — peak worker RAM N×4 DFs → ~4 DFs.
Plan 26: paper trading ($100, MAX_CONCURRENT 5), stock filter, BB/coil OR, bounce re-enabled.
Plan 27: report_worker subprocess — pandas out of main process, 42 MB permanent (−66%).
Plan 28: signal_log table — full funnel tracking (queued→filled/expired/invalidated/cancelled),
         price snapshots at 4h+24h for direction accuracy, /edge Telegram commands.
Plan 29: Analytics framework — /balance, /diagnose (4-layer ✓/⚠/✗ + N-gates), /edge pairs
         (blacklist flagging), /edge atr (ATR% environment split), post_be_tp1 column,
         tracking.json (user-defined periods), backtest funnel metrics, /stats by period.

**Gap to 10/10:** Live order execution, account balance auto-sync, min order value check.
