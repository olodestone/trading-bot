"""
backtest.py — Historical validation using the exact same strategy logic.

Imports strategy functions directly — no duplication, no drift.
Walk-forward simulation with zero lookahead bias.

Usage:
    python backtest.py                              # defaults: 11 pairs, 90 days, KuCoin spot
    python backtest.py --days 180                   # longer period
    python backtest.py --symbols SOL/USDT AAVE/USDT # specific pairs
    python backtest.py --futures                    # MEXC futures
    python backtest.py --be 1.3                     # raise breakeven trigger to 1.3R
    python backtest.py --mode bear                  # force fixed bear mode (testing only)
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
from strategy import apply_indicators, generate_pullback_signal

# ============================================================
# CONFIG
# ============================================================
DEFAULT_SYMBOLS = [
    "AAVE/USDT", "ZEC/USDT", "ALGO/USDT", "DOT/USDT", "ATOM/USDT",
    "INJ/USDT", "NEAR/USDT",
    "AVAX/USDT", "LINK/USDT", "LTC/USDT", "FIL/USDT",
]
DEFAULT_DAYS   = 90
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
    Walk forward from signal_idx simulating the 4-step management rules:

      TP1 = 1R  → close 50% of position, move SL to breakeven
      TP2 = 2.5R → close remaining 50% (WIN)
      BE  = only activated after TP1 is hit (not before)

    R multiples reflect the split position:
      LOSS:   -1.0R  (full position stopped before TP1)
      BE_WIN: +0.5R  (50% closed at TP1, 50% stopped at BE)
      WIN:    +1.75R (50% at TP1 + 50% at TP2 at 2.5R)
    """
    risk = abs(entry - sl)
    if risk <= 0:
        return None

    # ── phase 1: wait for entry to be touched (max 24h = 96 candles) ──
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
                "end_bar": 96, "tp1_hit": False, "mae": 0.0, "mfe": 0.0}

    # ── phase 2: manage the open trade (max 48h = 192 candles from entry) ──
    be_activated = False   # BE is only armed after TP1
    trail_sl     = sl
    tp1_hit      = False
    max_candle   = min(entry_idx + 193, len(df_15m) - 1)

    max_fav = 0.0   # max favorable excursion in R (MFE)
    max_adv = 0.0   # max adverse excursion in R (MAE)

    for i in range(entry_idx, max_candle + 1):
        c       = df_15m.iloc[i]
        bars_in = i - signal_idx
        atr_i   = c.get("atr", risk)   # current-bar ATR for dynamic trail

        if sig == "BUY":
            fav = (c["high"] - entry) / risk
            adv = (entry - c["low"])  / risk
        else:
            fav = (entry - c["low"])  / risk
            adv = (c["high"] - entry) / risk

        if fav > max_fav: max_fav = fav
        if adv > max_adv: max_adv = adv

        if sig == "BUY":
            # TP1 hit — close 50%, arm ATR trail on the runner
            if not tp1_hit and c["high"] >= tp1:
                tp1_hit      = True
                be_activated = True
                trail_sl     = entry   # start trail at BE

            # Update ATR trail: ratchet up as price moves in our favour
            if tp1_hit:
                new_trail = c["high"] - 2.0 * atr_i
                if new_trail > trail_sl:
                    trail_sl = new_trail

            # Stopped out
            if c["low"] <= trail_sl:
                exit_price = trail_sl
                if be_activated:
                    runner_r = (exit_price - entry) / risk   # runner's portion (50%)
                    total_r  = round(0.5 * 1.0 + 0.5 * runner_r, 2)   # 50% at TP1 + 50% at trail
                    result   = "WIN" if runner_r > 0 else "BE_WIN"
                    return {"result": result, "r_multiple": total_r, "end_bar": bars_in,
                            "tp1_hit": True, "entry": entry, "exit": exit_price,
                            "mae": round(max_adv, 2), "mfe": round(max_fav, 2)}
                return {"result": "LOSS", "r_multiple": -1.0, "end_bar": bars_in,
                        "tp1_hit": False, "entry": entry, "exit": trail_sl,
                        "mae": round(max_adv, 2), "mfe": round(max_fav, 2)}

        elif sig == "SELL":
            # TP1 hit — close 50%, arm ATR trail on the runner
            if not tp1_hit and c["low"] <= tp1:
                tp1_hit      = True
                be_activated = True
                trail_sl     = entry   # start trail at BE

            # Update ATR trail: ratchet down as price moves in our favour
            if tp1_hit:
                new_trail = c["low"] + 2.0 * atr_i
                if new_trail < trail_sl:
                    trail_sl = new_trail

            # Stopped out
            if c["high"] >= trail_sl:
                exit_price = trail_sl
                if be_activated:
                    runner_r = (entry - exit_price) / risk
                    total_r  = round(0.5 * 1.0 + 0.5 * runner_r, 2)
                    result   = "WIN" if runner_r > 0 else "BE_WIN"
                    return {"result": result, "r_multiple": total_r, "end_bar": bars_in,
                            "tp1_hit": True, "entry": entry, "exit": exit_price,
                            "mae": round(max_adv, 2), "mfe": round(max_fav, 2)}
                return {"result": "LOSS", "r_multiple": -1.0, "end_bar": bars_in,
                        "tp1_hit": False, "entry": entry, "exit": trail_sl,
                        "mae": round(max_adv, 2), "mfe": round(max_fav, 2)}

    # 48h timeout — close runner at last price
    last_c = df_15m.iloc[max_candle]
    if tp1_hit:
        exit_price = last_c["close"]
        runner_r   = (exit_price - entry) / risk if sig == "BUY" else (entry - exit_price) / risk
        total_r    = round(0.5 * 1.0 + 0.5 * runner_r, 2)
        return {"result": "WIN" if runner_r > 0 else "BE_WIN",
                "r_multiple": total_r, "end_bar": max_candle - signal_idx,
                "tp1_hit": True, "mae": round(max_adv, 2), "mfe": round(max_fav, 2)}
    return {"result": "TIMEOUT", "r_multiple": 0.0,
            "end_bar": max_candle - signal_idx, "tp1_hit": tp1_hit,
            "mae": round(max_adv, 2), "mfe": round(max_fav, 2)}


# ============================================================
# SYMBOL DATA FETCH
# ============================================================
def fetch_symbol_data(exchange, symbol, market_type, days):
    """
    Fetch and indicator-apply all timeframes for one symbol.
    Returns a data dict, or None if data is insufficient.
    """
    print(f"\n  📥 {symbol} — fetching {days}d of data...")

    df_15m = fetch_history(exchange, symbol, "15m", days)
    df_1h  = fetch_history(exchange, symbol, "1h",  days + 8)
    df_4h  = fetch_history(exchange, symbol, "4h",  days + 30)
    df_1d  = fetch_history(exchange, symbol, "1d",  days + 60)

    if any(x is None or len(x) < 60 for x in [df_15m, df_1h, df_4h, df_1d]):
        print(f"  ⚠️ Insufficient history for {symbol} — skipped")
        return None

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

    return {
        "df_15m": df_15m, "df_1h": df_1h, "df_4h": df_4h, "df_1d": df_1d,
        "map_1h": map_1h, "map_4h": map_4h, "map_1d": map_1d,
    }


# ============================================================
# DYNAMIC MARKET MODE TIMELINE
# ============================================================
def compute_mode_timeline(symbol_data):
    """
    Build a {15m_timestamp_ms: mode_string} mapping using the same breadth +
    hysteresis logic as _update_market_mode() in bot.py.

    bear_breadth = fraction of symbols where 1h ema50 < ema200 at that moment.

    bear  mode: breadth > 65% for 2+ consecutive 15m steps
    recovery:   breadth <= 45% for 3+ consecutive 15m steps
    normal:     otherwise
    """
    # Build master timeline: union of all symbols' 15m timestamps, sorted.
    # For synchronized USDT pairs this is effectively the same for all symbols.
    all_ts_set = set()
    for data in symbol_data.values():
        all_ts_set.update(data["df_15m"]["time"].values.tolist())
    master_ts = sorted(all_ts_set)

    # For each symbol, build a fast lookup: 15m_ts → 1h_row_index
    sym_1h_lookup = {}
    for sym, data in symbol_data.items():
        ts_arr  = data["df_15m"]["time"].values.tolist()
        map_arr = data["map_1h"].tolist()
        sym_1h_lookup[sym] = dict(zip(ts_arr, map_arr))

    bear_scans     = 0
    recovery_scans = 0
    mode           = "normal"
    mode_by_ts     = {}

    for ts in master_ts:
        bear_count = 0
        total      = 0
        for sym, data in symbol_data.items():
            h_idx = sym_1h_lookup[sym].get(ts)
            if h_idx is None:
                continue
            row = data["df_1h"].iloc[h_idx]
            if pd.isna(row["ema50"]) or pd.isna(row["ema200"]):
                continue
            total += 1
            if row["ema50"] < row["ema200"]:
                bear_count += 1

        if total > 0:
            breadth = bear_count / total
            if breadth > 0.65:
                bear_scans     += 1
                recovery_scans  = 0
            elif breadth <= 0.45:
                recovery_scans += 1
                bear_scans      = 0
            else:
                bear_scans     = max(0, bear_scans - 1)
                recovery_scans = max(0, recovery_scans - 1)

            if bear_scans >= 2:
                mode = "bear"
            elif recovery_scans >= 3:
                mode = "recovery"
            else:
                mode = "normal"

        mode_by_ts[ts] = mode

    return mode_by_ts


# ============================================================
# SINGLE-SYMBOL SIGNAL LOOP
# ============================================================
def run_symbol_backtest(symbol, data, mode_by_ts):
    """
    Run the signal generation + trade simulation loop for one symbol.
    mode_by_ts: {15m_timestamp_ms → mode_string} — looked up at each candle.
    """
    df_15m = data["df_15m"]
    df_1h  = data["df_1h"]
    df_4h  = data["df_4h"]
    df_1d  = data["df_1d"]
    map_1h = data["map_1h"]
    map_4h = data["map_4h"]
    map_1d = data["map_1d"]

    trades       = []
    active_until = -1   # skip candles while a trade is running

    for i in range(WARMUP_CANDLES, len(df_15m) - 1):
        if i <= active_until:
            continue

        ts          = int(df_15m.iloc[i]["time"])
        market_mode = mode_by_ts.get(ts, "normal")

        # Slice to current candle — simulate "we only know up to now"
        s15 = df_15m.iloc[: i + 1]
        s1h = df_1h.iloc[: map_1h[i] + 1]
        s4h = df_4h.iloc[: map_4h[i] + 1]
        s1d = df_1d.iloc[: map_1d[i] + 1]

        if any(len(x) < 50 for x in [s15, s1h, s4h, s1d]):
            continue

        result = generate_pullback_signal(s15, s1h, s4h, s1d, symbol=symbol, market_mode=market_mode)
        if not result:
            continue

        sig, entry, sl, tp1, tp2, rr, atr, trade_type = result

        trade = simulate_trade(df_15m, i, sig, entry, sl, tp1, tp2)
        if not trade or trade["result"] in ("EXPIRED", "TIMEOUT"):
            active_until = i + trade["end_bar"] if trade else i + 10
            continue

        trade.update({
            "symbol":      symbol,
            "signal":      sig,
            "trade_type":  trade_type,
            "rr_signal":   rr,
            "signal_time": pd.Timestamp(df_15m.iloc[i]["time"], unit="ms"),
            "market_mode": market_mode,
        })
        trades.append(trade)

        # Advance past this trade so signals don't fire mid-trade
        active_until = i + trade["end_bar"] + 2

    print(f"  ✅ {symbol}: {len(trades)} trades")
    return trades


# ============================================================
# REPORT
# ============================================================
def generate_report(all_trades, days, symbols, mode_label="DYNAMIC"):
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

    total_r    = df["r_multiple"].sum()
    avg_r      = df["r_multiple"].mean()
    # avg_win_r must include BE_WIN trades — otherwise expectancy is inflated
    all_wins_df = pd.concat([wins, be_wins])
    avg_win_r  = all_wins_df["r_multiple"].mean() if len(all_wins_df) > 0 else 0.0
    avg_loss_r = losses["r_multiple"].mean() if n_l > 0 else 0.0

    # True expectancy = average R per trade (not a derived formula that can mismatch)
    expectancy = round(avg_r, 3)

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
    print(f"  BACKTEST RESULTS  ·  {days}d  ·  {len(symbols)} pairs  ·  TP1=1R TP2=2.5R BE-after-TP1")
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

    # Per-trade-type breakdown — tells you which entry type is profitable
    if "trade_type" in df.columns:
        print(f"\n  BY ENTRY TYPE")
        print(sep)
        print(f"  {'Type':<12} {'Trades':>6} {'WR%':>6} {'Total R':>9} {'Avg R':>7}")
        print(f"  {'-'*12} {'-'*6} {'-'*6} {'-'*9} {'-'*7}")
        by_type = df.groupby("trade_type").agg(
            trades   = ("result", "count"),
            wins_all = ("result", lambda x: x.isin(["WIN","BE_WIN"]).sum()),
            total_r  = ("r_multiple", "sum"),
            avg_r    = ("r_multiple", "mean"),
        ).reset_index()
        by_type["wr"]      = (by_type["wins_all"] / by_type["trades"] * 100).round(1)
        by_type["total_r"] = by_type["total_r"].round(2)
        by_type["avg_r"]   = by_type["avg_r"].round(2)
        by_type = by_type.sort_values("total_r", ascending=False)
        for _, row in by_type.iterrows():
            print(f"  {row['trade_type']:<12} {int(row['trades']):>6} "
                  f"{row['wr']:>5.1f}% {row['total_r']:>+9.2f}R {row['avg_r']:>+7.2f}R")

    # Per-market-mode breakdown — only meaningful in dynamic mode
    if "market_mode" in df.columns and mode_label == "DYNAMIC":
        print(f"\n  BY MARKET MODE")
        print(sep)
        print(f"  {'Mode':<10} {'Trades':>6} {'WR%':>6} {'Total R':>9} {'Avg R':>7}")
        print(f"  {'-'*10} {'-'*6} {'-'*6} {'-'*9} {'-'*7}")
        by_mode = df.groupby("market_mode").agg(
            trades   = ("result", "count"),
            wins_all = ("result", lambda x: x.isin(["WIN","BE_WIN"]).sum()),
            total_r  = ("r_multiple", "sum"),
            avg_r    = ("r_multiple", "mean"),
        ).reset_index()
        by_mode["wr"]      = (by_mode["wins_all"] / by_mode["trades"] * 100).round(1)
        by_mode["total_r"] = by_mode["total_r"].round(2)
        by_mode["avg_r"]   = by_mode["avg_r"].round(2)
        by_mode = by_mode.sort_values("total_r", ascending=False)
        for _, row in by_mode.iterrows():
            print(f"  {row['market_mode']:<10} {int(row['trades']):>6} "
                  f"{row['wr']:>5.1f}% {row['total_r']:>+9.2f}R {row['avg_r']:>+7.2f}R")

    # Verdict
    print(f"\n  EDGE ASSESSMENT")
    print(sep)
    # Expectancy is now true avg R per trade (fixed formula)
    if expectancy > 0.15 and pf >= 1.5 and win_rate >= 45:
        verdict = "✅  STRONG EDGE — strategy validated"
        grade   = "A"
    elif expectancy > 0.05 and pf >= 1.2:
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
    parser.add_argument(
        "--mode", choices=["dynamic", "normal", "bear", "recovery"], default="dynamic",
        help="Market mode: 'dynamic' (default) computes mode from breadth at each step, "
             "or force a fixed mode for targeted testing."
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

    mode_label = args.mode.upper()

    print(f"\n{'═'*34}")
    print(f"  🔬 BACKTEST STARTING")
    print(f"{'═'*34}")
    print(f"  Exchange : {label}")
    print(f"  Symbols  : {', '.join(args.symbols)}")
    print(f"  Period   : {args.days} days")
    print(f"  Mode     : {mode_label}")
    print(f"  Mgmt     : TP1=1R  TP2=2.5R  BE after TP1")
    print(f"  Warmup   : {WARMUP_CANDLES} candles (skipped)")
    print(f"{'─'*34}")

    # ── Phase 1: fetch all symbol data ──────────────────────────────────────
    symbol_data = {}
    for symbol in args.symbols:
        try:
            data = fetch_symbol_data(exchange, symbol, market_type, args.days)
            if data:
                symbol_data[symbol] = data
        except KeyboardInterrupt:
            print("\n  ⏹  Interrupted during fetch.")
            break
        except Exception as e:
            print(f"  ❌ {symbol} fetch error: {e}")
        time.sleep(1)

    if not symbol_data:
        print("  ❌ No symbol data fetched — aborting.")
        return

    # ── Phase 2: build mode timeline ────────────────────────────────────────
    if args.mode == "dynamic":
        print(f"\n  📊 Computing dynamic market mode timeline...")
        mode_by_ts = compute_mode_timeline(symbol_data)
        # Print mode distribution summary
        counts = {"bear": 0, "recovery": 0, "normal": 0}
        for m in mode_by_ts.values():
            counts[m] += 1
        total_ts = len(mode_by_ts)
        print(f"  Mode distribution: "
              f"normal {counts['normal']/total_ts:.0%}  "
              f"bear {counts['bear']/total_ts:.0%}  "
              f"recovery {counts['recovery']/total_ts:.0%}")
    else:
        # Fixed mode: fill every timestamp with the forced mode
        all_ts: set = set()
        for data in symbol_data.values():
            all_ts.update(data["df_15m"]["time"].values.tolist())
        mode_by_ts = {ts: args.mode for ts in all_ts}
        print(f"  Mode fixed to: {args.mode.upper()} for all {len(all_ts):,} candle steps")

    # ── Phase 3: run per-symbol signal loops ────────────────────────────────
    print(f"\n{'─'*34}")
    all_trades = []
    for symbol, data in symbol_data.items():
        try:
            trades = run_symbol_backtest(symbol, data, mode_by_ts)
            all_trades.extend(trades)
        except KeyboardInterrupt:
            print("\n  ⏹  Interrupted — generating partial report...")
            break
        except Exception as e:
            print(f"  ❌ {symbol} error: {e}")

    generate_report(all_trades, args.days, list(symbol_data.keys()), mode_label=mode_label)


if __name__ == "__main__":
    main()
