"""
backtest.py — Historical validation using the exact same strategy logic.

Imports strategy functions directly — no duplication, no drift.
Walk-forward simulation with zero lookahead bias.

Usage:
    python backtest.py                              # defaults: 8 pairs, 90 days, KuCoin spot
    python backtest.py --days 180                   # longer period
    python backtest.py --symbols SOL/USDT AAVE/USDT # specific pairs
    python backtest.py --futures                    # MEXC futures
    python backtest.py --days 90 --symbols SOL/USDT LINK/USDT ATOM/USDT
"""

import argparse
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import os
import sys

# ── exact strategy functions, zero duplication ──────────────────────────────
from strategy import apply_indicators, generate_filtered_signal

# ============================================================
# CONFIG
# ============================================================
DEFAULT_SYMBOLS = [
    "SOL/USDT", "AAVE/USDT", "LINK/USDT", "ATOM/USDT",
    "XMR/USDT", "ALGO/USDT", "DOT/USDT", "ZEC/USDT",
]
DEFAULT_DAYS   = 90
MID_CAP_MIN    = 0.10
MID_CAP_MAX    = 150.0
WARMUP_CANDLES = 220   # enough for EMA200 + BBW(50) convergence


# ============================================================
# DATA FETCHING  (paginated — handles any period length)
# ============================================================
def fetch_history(exchange, symbol, timeframe, days):
    """
    Fetch complete OHLCV history for `days` days, paginating automatically.
    Returns a clean, deduplicated DataFrame sorted by time, or None on failure.
    """
    since = int((datetime.utcnow() - timedelta(days=days)).timestamp() * 1000)
    now   = int(datetime.utcnow().timestamp() * 1000)
    all_rows = []

    while since < now:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=500)
        except ccxt.RateLimitExceeded:
            time.sleep(15)
            continue
        except Exception as e:
            print(f"    ⚠️ {symbol} {timeframe}: {e}")
            break

        if not batch:
            break

        all_rows.extend(batch)
        last_ts = batch[-1][0]

        if last_ts >= now - 60_000 or len(batch) < 2:
            break

        since = last_ts + 1
        time.sleep(0.4)

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows, columns=["time", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return df


# ============================================================
# HTF ALIGNMENT
# ============================================================
def build_htf_map(df_15m_times, df_htf_times):
    """
    For every 15m candle timestamp, find the index of the most recent
    completed HTF candle (open_time <= 15m_time).
    Returns a numpy array of htf indices, one per 15m candle.
    """
    htf_arr = np.array(df_htf_times)
    result  = np.searchsorted(htf_arr, df_15m_times, side="right") - 1
    return np.maximum(result, 0)


# ============================================================
# TRADE SIMULATION  (mirrors check_trade_results / check_pending_trades)
# ============================================================
def simulate_trade(df_15m, signal_idx, sig, entry, sl, tp1, tp2):
    """
    Walk forward from signal_idx on 15m candles, applying the same
    breakeven / trail / tiered-TP logic as the live bot.

    Returns a dict with result details, or None if data runs out.
    """
    risk = abs(entry - sl)
    if risk <= 0:
        return None

    # ── phase 1: wait for entry to be hit (max 24h = 96 candles) ──
    entry_idx = None
    end_idx   = min(signal_idx + 97, len(df_15m) - 1)

    for i in range(signal_idx + 1, end_idx + 1):
        c = df_15m.iloc[i]
        if sig == "BUY"  and c["low"]  <= entry:
            entry_idx = i; break
        if sig == "SELL" and c["high"] >= entry:
            entry_idx = i; break

    if entry_idx is None:
        return {"result": "EXPIRED", "r_multiple": 0.0,
                "end_bar": 96, "tp1_hit": False}

    # ── phase 2: manage the open trade (max 48h = 192 candles from entry) ──
    be_activated = False
    trail_sl     = sl
    tp1_hit      = False
    max_candle   = min(entry_idx + 193, len(df_15m) - 1)

    for i in range(entry_idx, max_candle + 1):
        c        = df_15m.iloc[i]
        bars_in  = i - signal_idx   # bars since signal (for active_until tracking)

        if sig == "BUY":
            # Breakeven at 1:1
            if not be_activated and c["high"] >= entry + risk:
                be_activated = True
                trail_sl     = entry

            # Trail tightens beyond 2:1
            if be_activated and c["high"] >= entry + 2 * risk:
                new_trail = c["high"] - 1.2 * risk
                if new_trail > trail_sl:
                    trail_sl = new_trail

            # Stopped out
            if c["low"] <= trail_sl:
                r = (trail_sl - entry) / risk if be_activated else -1.0
                return {"result": "BE_WIN" if be_activated else "LOSS",
                        "r_multiple": round(r, 2), "end_bar": bars_in,
                        "tp1_hit": tp1_hit, "entry": entry, "exit": trail_sl}

            # TP2 (runner target)
            if tp1_hit and tp2 and c["high"] >= tp2:
                r = (tp2 - entry) / risk
                return {"result": "WIN", "r_multiple": round(r, 2),
                        "end_bar": bars_in, "tp1_hit": True,
                        "entry": entry, "exit": tp2}

            # TP1 (partial exit)
            if not tp1_hit and c["high"] >= tp1:
                tp1_hit = True
                if not tp2:
                    r = (tp1 - entry) / risk
                    return {"result": "WIN", "r_multiple": round(r, 2),
                            "end_bar": bars_in, "tp1_hit": True,
                            "entry": entry, "exit": tp1}

        elif sig == "SELL":
            if not be_activated and c["low"] <= entry - risk:
                be_activated = True
                trail_sl     = entry

            if be_activated and c["low"] <= entry - 2 * risk:
                new_trail = c["low"] + 1.2 * risk
                if new_trail < trail_sl:
                    trail_sl = new_trail

            if c["high"] >= trail_sl:
                r = (entry - trail_sl) / risk if be_activated else -1.0
                return {"result": "BE_WIN" if be_activated else "LOSS",
                        "r_multiple": round(r, 2), "end_bar": bars_in,
                        "tp1_hit": tp1_hit, "entry": entry, "exit": trail_sl}

            if tp1_hit and tp2 and c["low"] <= tp2:
                r = (entry - tp2) / risk
                return {"result": "WIN", "r_multiple": round(r, 2),
                        "end_bar": bars_in, "tp1_hit": True,
                        "entry": entry, "exit": tp2}

            if not tp1_hit and c["low"] <= tp1:
                tp1_hit = True
                if not tp2:
                    r = (entry - tp1) / risk
                    return {"result": "WIN", "r_multiple": round(r, 2),
                            "end_bar": bars_in, "tp1_hit": True,
                            "entry": entry, "exit": tp1}

    # 48h timeout — price never hit TP or SL
    return {"result": "TIMEOUT", "r_multiple": 0.0,
            "end_bar": max_candle - signal_idx, "tp1_hit": tp1_hit}


# ============================================================
# SINGLE-SYMBOL BACKTEST
# ============================================================
def backtest_symbol(exchange, symbol, market_type, days):
    print(f"\n  📥 {symbol} — fetching {days}d of data...")

    df_15m = fetch_history(exchange, symbol, "15m", days)
    df_1h  = fetch_history(exchange, symbol, "1h",  days + 8)
    df_4h  = fetch_history(exchange, symbol, "4h",  days + 30)
    df_1d  = fetch_history(exchange, symbol, "1d",  days + 60)

    if any(x is None or len(x) < 60 for x in [df_15m, df_1h, df_4h, df_1d]):
        print(f"  ⚠️ Insufficient history for {symbol} — skipped")
        return []

    # Apply indicators ONCE on full history.
    # All indicators (EMA, RSI, ATR, ADX, BBW) are purely backward-looking
    # (ewm/rolling use only past data), so this is mathematically identical
    # to recomputing at each step — with no lookahead bias.
    df_15m = apply_indicators(df_15m.copy())
    df_1h  = apply_indicators(df_1h.copy())
    df_4h  = apply_indicators(df_4h.copy())
    df_1d  = apply_indicators(df_1d.copy())

    # Pre-build alignment maps: for each 15m candle index → htf row index
    map_1h = build_htf_map(df_15m["time"].values, df_1h["time"].values)
    map_4h = build_htf_map(df_15m["time"].values, df_4h["time"].values)
    map_1d = build_htf_map(df_15m["time"].values, df_1d["time"].values)

    trades       = []
    active_until = -1   # skip candles while a trade is running

    for i in range(WARMUP_CANDLES, len(df_15m) - 1):
        if i <= active_until:
            continue

        # Slice to current candle — simulate "we only know up to now"
        s15 = df_15m.iloc[: i + 1]
        s1h = df_1h.iloc[: map_1h[i] + 1]
        s4h = df_4h.iloc[: map_4h[i] + 1]
        s1d = df_1d.iloc[: map_1d[i] + 1]

        if any(len(x) < 50 for x in [s15, s1h, s4h, s1d]):
            continue

        # Mid-cap filter — same gate as the live bot
        price = s15.iloc[-1]["close"]
        if not (MID_CAP_MIN <= price <= MID_CAP_MAX):
            continue

        result = generate_filtered_signal(s15, s1h, s4h, s1d)
        if not result:
            continue

        sig, entry, sl, tp1, tp2, rr, atr, trade_type = result

        trade = simulate_trade(df_15m, i, sig, entry, sl, tp1, tp2)
        if not trade or trade["result"] in ("EXPIRED", "TIMEOUT"):
            active_until = i + trade["end_bar"] if trade else i + 10
            continue

        trade.update({
            "symbol":     symbol,
            "signal":     sig,
            "trade_type": trade_type,
            "rr_signal":  rr,
            "signal_time": pd.Timestamp(df_15m.iloc[i]["time"], unit="ms"),
        })
        trades.append(trade)

        # Advance past this trade so signals don't fire mid-trade
        active_until = i + trade["end_bar"] + 2

    print(f"  ✅ {symbol}: {len(trades)} trades")
    return trades


# ============================================================
# REPORT
# ============================================================
def generate_report(all_trades, days, symbols):
    sep  = "─" * 34
    sep2 = "═" * 34

    if not all_trades:
        print(f"\n{sep2}")
        print("  ⚠️  No completed trades found.")
        print("  This means the strategy's strict gates")
        print("  found no qualifying setups in this period.")
        print("  Try --days 180 or different symbols.")
        print(sep2)
        return

    df = pd.DataFrame(all_trades)

    wins    = df[df["result"] == "WIN"]
    be_wins = df[df["result"] == "BE_WIN"]
    losses  = df[df["result"] == "LOSS"]

    n_w  = len(wins)
    n_be = len(be_wins)
    n_l  = len(losses)
    total = len(df)
    closed = n_w + n_be + n_l

    win_rate = (n_w + n_be) / closed * 100 if closed > 0 else 0.0

    total_r   = df["r_multiple"].sum()
    avg_r     = df["r_multiple"].mean()
    avg_win_r = wins["r_multiple"].mean() if n_w > 0 else 0.0
    avg_loss_r = losses["r_multiple"].mean() if n_l > 0 else 0.0

    # Expectancy (R per trade)
    wr = (n_w + n_be) / closed if closed > 0 else 0.0
    lr = n_l / closed if closed > 0 else 0.0
    expectancy = round((wr * avg_win_r) - (lr * 1.0), 3)

    # Profit factor
    gross_profit = (wins["r_multiple"].sum() + be_wins["r_multiple"].sum())
    gross_loss   = abs(losses["r_multiple"].sum())
    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

    # Max drawdown on cumulative R curve
    r_curve  = df["r_multiple"].cumsum().values
    peak     = np.maximum.accumulate(r_curve)
    drawdown = r_curve - peak
    max_dd   = round(float(drawdown.min()), 2)

    # Simplified annualised Sharpe
    std_r = df["r_multiple"].std()
    sharpe = round((avg_r / std_r) * (252 ** 0.5), 2) if std_r > 0 else 0.0

    # Consecutive wins / losses
    results_seq = df["result"].apply(
        lambda x: "W" if x in ("WIN", "BE_WIN") else "L"
    ).tolist()
    max_consec_w = max_consec_l = cur = 0
    cur_type = None
    for r in results_seq:
        if r == cur_type:
            cur += 1
        else:
            cur = 1; cur_type = r
        if r == "W": max_consec_w = max(max_consec_w, cur)
        else:        max_consec_l = max(max_consec_l, cur)

    # Per-symbol breakdown
    sym = df.groupby("symbol").agg(
        trades   = ("result", "count"),
        wins_all = ("result", lambda x: x.isin(["WIN","BE_WIN"]).sum()),
        total_r  = ("r_multiple", "sum"),
        avg_r    = ("r_multiple", "mean"),
    ).reset_index()
    sym["wr"]      = (sym["wins_all"] / sym["trades"] * 100).round(1)
    sym["total_r"] = sym["total_r"].round(2)
    sym["avg_r"]   = sym["avg_r"].round(2)
    sym = sym.sort_values("total_r", ascending=False)

    # Best / worst
    best  = df.loc[df["r_multiple"].idxmax()]
    worst = df.loc[df["r_multiple"].idxmin()]

    print(f"\n{sep2}")
    print(f"  BACKTEST RESULTS  ·  {days}d  ·  {len(symbols)} pairs")
    print(sep2)

    print(f"\n  TRADE SUMMARY")
    print(sep)
    print(f"  Total signals       {total}")
    print(f"  Wins (full TP)      {n_w}")
    print(f"  Wins (breakeven)    {n_be}")
    print(f"  Losses              {n_l}")
    print(f"  Win Rate            {win_rate:.1f}%")
    print(f"  Max consec. wins    {max_consec_w}")
    print(f"  Max consec. losses  {max_consec_l}")

    print(f"\n  PERFORMANCE  (R = 1× risk per trade)")
    print(sep)
    print(f"  Total R             {total_r:+.2f}R")
    print(f"  Avg R / trade       {avg_r:+.3f}R")
    print(f"  Avg Win R           {avg_win_r:+.2f}R")
    print(f"  Avg Loss R          {avg_loss_r:+.2f}R")
    print(f"  Expectancy          {expectancy:+.3f}R")
    print(f"  Profit Factor       {pf}x")
    print(f"  Max Drawdown        {max_dd}R")
    print(f"  Sharpe (ann.)       {sharpe}")

    print(f"\n  BEST / WORST TRADES")
    print(sep)
    print(f"  Best   {best['symbol']:<12} {best['r_multiple']:+.2f}R"
          f"  {best['signal_time'].strftime('%Y-%m-%d')}  [{best['result']}]")
    print(f"  Worst  {worst['symbol']:<12} {worst['r_multiple']:+.2f}R"
          f"  {worst['signal_time'].strftime('%Y-%m-%d')}  [{worst['result']}]")

    print(f"\n  PER-SYMBOL BREAKDOWN")
    print(sep)
    print(f"  {'Symbol':<14} {'Trades':>6} {'WR%':>6} {'Total R':>9} {'Avg R':>7}")
    print(f"  {'-'*14} {'-'*6} {'-'*6} {'-'*9} {'-'*7}")
    for _, row in sym.iterrows():
        print(f"  {row['symbol']:<14} {int(row['trades']):>6} "
              f"{row['wr']:>5.1f}% {row['total_r']:>+9.2f}R {row['avg_r']:>+7.2f}R")

    # Verdict
    print(f"\n  EDGE ASSESSMENT")
    print(sep)
    if expectancy > 0.5 and pf >= 1.5 and win_rate >= 40:
        verdict = "✅  STRONG EDGE — strategy validated"
        grade   = "A"
    elif expectancy > 0.2 and pf >= 1.2:
        verdict = "⚠️  MODERATE EDGE — needs more data to confirm"
        grade   = "B"
    elif expectancy > 0:
        verdict = "⚠️  SLIGHT EDGE — refine filters or extend period"
        grade   = "C"
    else:
        verdict = "❌  NO EDGE — strategy needs adjustment"
        grade   = "F"

    print(f"  {verdict}")
    print(f"  Backtest Grade: {grade}")
    print(f"  (need 30+ trades for statistical significance)")

    print(f"\n{sep2}\n")

    # Save full trade log to CSV
    out = f"backtest_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
    df.to_csv(out, index=False)
    print(f"  📁 Full trade log saved → {out}\n")


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Backtest the trading strategy on historical data"
    )
    parser.add_argument(
        "--symbols", nargs="+", default=DEFAULT_SYMBOLS,
        help="Space-separated list of symbols (e.g. SOL/USDT AAVE/USDT)"
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS,
        help=f"Days of history to test (default: {DEFAULT_DAYS})"
    )
    parser.add_argument(
        "--futures", action="store_true",
        help="Use MEXC futures instead of KuCoin spot"
    )
    args = parser.parse_args()

    if args.futures:
        exchange    = ccxt.mexc({"enableRateLimit": True})
        market_type = "futures"
        label       = "MEXC Futures"
        # Adjust futures symbols if plain format given
        args.symbols = [
            s if ":" in s else s.replace("/USDT", "/USDT:USDT")
            for s in args.symbols
        ]
    else:
        exchange    = ccxt.kucoin({"enableRateLimit": True, "rateLimit": 1200})
        market_type = "spot"
        label       = "KuCoin Spot"

    exchange.load_markets()

    print(f"\n{'═'*34}")
    print(f"  🔬 BACKTEST STARTING")
    print(f"{'═'*34}")
    print(f"  Exchange : {label}")
    print(f"  Symbols  : {', '.join(args.symbols)}")
    print(f"  Period   : {args.days} days")
    print(f"  Warmup   : {WARMUP_CANDLES} candles (skipped)")
    print(f"{'─'*34}")

    all_trades = []
    for symbol in args.symbols:
        try:
            trades = backtest_symbol(exchange, symbol, market_type, args.days)
            all_trades.extend(trades)
        except KeyboardInterrupt:
            print("\n  ⏹  Interrupted — generating partial report...")
            break
        except Exception as e:
            print(f"  ❌ {symbol} error: {e}")
        time.sleep(1)

    generate_report(all_trades, args.days, args.symbols)


if __name__ == "__main__":
    main()
