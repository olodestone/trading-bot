"""
Stats-only module — intentionally NOT imported at bot startup.

Bot lazy-imports this file once per day (daily_report) and on /stats command.
Pandas loads into the main process only then, and stays resident, which is
acceptable for once-daily use.

All hot-path DB operations (save_trade, check_trade_results, pending trades,
compounded balance, daily loss count) live in db.py without pandas.
"""
import os
import re
import time
import requests
import pandas as pd
from datetime import datetime
from db import (get_engine, TRADES_TABLE, PENDING_TABLE, SIGNAL_LOG_TABLE,
                get_signals_for_outcome_analysis, update_signal_outcome)


def _current_plan() -> int | None:
    """Return highest Plan N from CLAUDE.md, or None on failure."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CLAUDE.md")
        with open(path) as f:
            nums = re.findall(r"^## Plan (\d+)", f.read(), re.MULTILINE)
        return max(int(n) for n in nums) if nums else None
    except Exception:
        return None


def _fmt(p):
    if p >= 1000:   return f"{p:,.2f}"
    if p >= 1:      return f"{p:.4f}"
    if p >= 0.01:   return f"{p:.6f}"
    if p >= 0.0001: return f"{p:.8f}"
    return f"{p:.10f}"


def _expectancy(wins, be_wins, losses, avg_win_rr, avg_be_rr=0.0):
    total = wins + be_wins + losses
    if total == 0:
        return 0.0
    return round((wins * avg_win_rr + be_wins * avg_be_rr - losses * 1.0) / total, 3)


def _mae_mfe_section(df_closed):
    if df_closed.empty or "mfe" not in df_closed.columns:
        return ""
    df = df_closed.dropna(subset=["mfe", "mae"])
    if df.empty:
        return ""
    wins   = df[df["status"] == "WIN"]
    be     = df[df["status"] == "BE_WIN"]
    losses = df[df["status"] == "LOSS"]
    lines  = ["\nMAE/MFE:"]

    def _ttf(sub, col):
        return sub[col].dropna().mean() if col in sub.columns and not sub[col].dropna().empty else 0.0

    if not wins.empty:
        lines.append(
            f"  Wins ({len(wins)}):  avg MFE {wins['mfe'].mean():.2f}R  "
            f"peak @{_ttf(wins, 'time_to_mfe'):.1f}h"
        )
    if not be.empty:
        lines.append(
            f"  BE   ({len(be)}):  avg MFE {be['mfe'].mean():.2f}R  "
            f"peak @{_ttf(be, 'time_to_mfe'):.1f}h  <- exits too early?"
        )
    if not losses.empty:
        lines.append(
            f"  Loss ({len(losses)}):  avg MFE {losses['mfe'].mean():.2f}R  "
            f"avg MAE {losses['mae'].mean():.2f}R  "
            f"SL hit @{_ttf(losses, 'time_to_mae'):.1f}h"
        )
    return "\n".join(lines) if len(lines) > 1 else ""


# ──────────────────────────────────────────────────────────────────────────────
# STATS  (pandas-heavy — only called on /stats command)
# ──────────────────────────────────────────────────────────────────────────────
def get_stats_summary():
    try:
        engine = get_engine()
        df = pd.read_sql(f"SELECT * FROM {TRADES_TABLE}", engine)
    except Exception as e:
        return f"Stats error: {e}"

    if df.empty:
        return "STATS\n\nNo closed trades yet."

    all_closed = df[df["status"].isin(["WIN", "BE_WIN", "LOSS"])].copy()
    if all_closed.empty:
        return "STATS\n\nNo closed trades yet."

    # Headline uses current plan only; falls back to all-time if no plan data
    cur_plan = _current_plan()
    if cur_plan is not None and "plan" in df.columns:
        headline_df = all_closed[all_closed["plan"] == cur_plan]
        plan_label  = f"PLAN {cur_plan} STATS"
    else:
        headline_df = all_closed
        plan_label  = "ALL-TIME STATS"

    if headline_df.empty:
        # New plan just started — show waiting message + historical by-plan below
        history_section = _plan_breakdown(all_closed, cur_plan)
        return (
            f"{plan_label}\n{'─'*22}\n"
            f"No closed trades yet — watching for Plan {cur_plan} results.\n"
            f"{history_section}"
        )

    wins       = len(headline_df[headline_df["status"] == "WIN"])
    be_wins    = len(headline_df[headline_df["status"] == "BE_WIN"])
    losses     = len(headline_df[headline_df["status"] == "LOSS"])
    total      = wins + be_wins + losses
    win_rate   = (wins + be_wins) / total * 100
    avg_rr     = headline_df[headline_df["status"] == "WIN"]["rr"].mean() if wins > 0 else 0.0
    best_rr    = headline_df[headline_df["status"] == "WIN"]["rr"].max()  if wins > 0 else 0.0
    avg_be_rr  = (headline_df[headline_df["status"] == "BE_WIN"]["rr"] * 0.5).mean() if be_wins > 0 else 0.0
    exp        = _expectancy(wins, be_wins, losses, avg_rr, avg_be_rr)

    headline_df = headline_df.sort_values(
        pd.to_datetime(headline_df["time"], errors="coerce")
    ) if "time" in headline_df.columns else headline_df
    results = headline_df["status"].apply(
        lambda s: "W" if s in ("WIN", "BE_WIN") else "L"
    ).tolist()

    cur_streak = 1 if results else 0
    for i in range(len(results) - 1, 0, -1):
        if results[i] == results[i - 1]:
            cur_streak += 1
        else:
            break
    streak_label = (f"{cur_streak}W" if results and results[-1] == "W"
                    else f"{cur_streak}L" if results else "—")

    edge_note = "Positive edge" if exp > 0 else "No edge yet -- keep tracking"
    sample_note = "" if total >= 30 else f"\n⚠ {total}/30 trades — early sample"

    conf_section = ""
    if "confidence" in headline_df.columns:
        conf_df = headline_df[headline_df["confidence"].notna()].copy()
        conf_df["confidence"] = conf_df["confidence"].astype(int)
        if len(conf_df) >= 5:
            buckets = [("80-100", 80, 100), ("65-79", 65, 79),
                       ("50-64", 50, 64), ("35-49", 35, 49), ("0-34", 0, 34)]
            lines = []
            for label, lo, hi in buckets:
                sub = conf_df[(conf_df["confidence"] >= lo) & (conf_df["confidence"] <= hi)]
                if len(sub) == 0:
                    continue
                sw     = len(sub[sub["status"] == "WIN"])
                sb     = len(sub[sub["status"] == "BE_WIN"])
                sl     = len(sub[sub["status"] == "LOSS"])
                swr    = (sw + sb) / len(sub) * 100
                s_rr   = sub[sub["status"] == "WIN"]["rr"].mean() if sw > 0 else 0.0
                s_be   = (sub[sub["status"] == "BE_WIN"]["rr"] * 0.5).mean() if sb > 0 else 0.0
                s_exp  = _expectancy(sw, sb, sl, s_rr, s_be)
                lines.append(f"  {label}  WR {swr:.0f}%  {s_exp:+.2f}R  ({len(sub)})")
            if lines:
                conf_section = f"\n{'─'*22}\nBy confidence:\n" + "\n".join(lines)

    history_section = _plan_breakdown(all_closed, cur_plan)

    return (
        f"{plan_label}\n{'─'*22}\n"
        f"Trades   {total} closed\n"
        f"W: {wins}  BE: {be_wins}  L: {losses}\n"
        f"Win Rate {win_rate:.1f}%\n"
        f"Avg RR   {avg_rr:.2f}\n"
        f"Best RR  {best_rr:.2f}\n{'─'*22}\n"
        f"Expectancy  {exp:+.3f}R\n"
        f"{edge_note}\n"
        f"Streak   {streak_label}"
        f"{sample_note}"
        f"{conf_section}"
        f"{history_section}"
    )


def _plan_breakdown(closed_df: "pd.DataFrame", cur_plan: int | None) -> str:
    """Return a 'By plan' section string, always showing all plans."""
    if "plan" not in closed_df.columns or closed_df.empty:
        return ""
    labelled = closed_df.copy()
    # Determine the label for NULL plan rows (pre-current-plan history)
    null_label = f"pre-{cur_plan}" if cur_plan is not None else "pre-plan"
    labelled["plan_label"] = labelled["plan"].apply(
        lambda p: f"Plan {int(p)}" if pd.notna(p) else null_label
    )
    unique = sorted(labelled["plan_label"].unique())
    if len(unique) <= 1 and cur_plan is not None:
        # Only one plan exists — no breakdown adds value yet
        return ""
    lines = []
    for label in unique:
        sub = labelled[labelled["plan_label"] == label]
        sw   = len(sub[sub["status"] == "WIN"])
        sb   = len(sub[sub["status"] == "BE_WIN"])
        sl_  = len(sub[sub["status"] == "LOSS"])
        swr  = (sw + sb) / len(sub) * 100
        s_rr = sub[sub["status"] == "WIN"]["rr"].mean() if sw > 0 else 0.0
        s_be = (sub[sub["status"] == "BE_WIN"]["rr"] * 0.5).mean() if sb > 0 else 0.0
        s_exp = _expectancy(sw, sb, sl_, s_rr, s_be)
        lines.append(
            f"  {label:<10}  W{sw} BE{sb} L{sl_}  "
            f"WR {swr:.0f}%  {s_exp:+.2f}R"
        )
    return f"\n{'─'*22}\nBy plan:\n" + "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# DAILY REPORT  (pandas-heavy — only called once per day)
# ──────────────────────────────────────────────────────────────────────────────
def daily_report(send_telegram):
    engine = get_engine()
    df = pd.read_sql(f"SELECT * FROM {TRADES_TABLE}", engine)

    if df.empty:
        send_telegram("DAILY REPORT\n\nNo trades yet.")
        return

    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    today      = datetime.utcnow().date()
    df_today   = df[df["time"].dt.date == today]

    wins    = len(df_today[df_today["status"] == "WIN"])
    be_wins = len(df_today[df_today["status"] == "BE_WIN"])
    losses  = len(df_today[df_today["status"] == "LOSS"])
    open_t  = len(df_today[df_today["status"] == "OPEN"])
    closed  = wins + be_wins + losses
    winrate = ((wins + be_wins) / closed * 100) if closed > 0 else 0
    avg_rr  = df_today[df_today["status"] == "WIN"]["rr"].mean() if wins > 0 else 0.0

    all_wins = len(df[df["status"] == "WIN"])
    all_be   = len(df[df["status"] == "BE_WIN"])
    all_loss = len(df[df["status"] == "LOSS"])
    all_cl   = all_wins + all_be + all_loss
    all_wr   = ((all_wins + all_be) / all_cl * 100) if all_cl > 0 else 0
    all_rr   = df[df["status"] == "WIN"]["rr"].mean()       if all_wins > 0 else 0.0
    all_be_rr= (df[df["status"] == "BE_WIN"]["rr"] * 0.5).mean() if all_be > 0 else 0.0
    all_exp  = _expectancy(all_wins, all_be, all_loss, all_rr, all_be_rr)

    df_closed_today = df_today[df_today["status"].isin(["WIN", "BE_WIN", "LOSS"])]
    mae_mfe = _mae_mfe_section(df_closed_today)

    send_telegram(
        f"DAILY REPORT ({today})\n\n"
        f"Today:\n"
        f"  Open: {open_t} | Closed: {closed}\n"
        f"  W: {wins}  BE: {be_wins}  L: {losses}\n"
        f"  Win Rate: {round(winrate,1)}%\n"
        f"  Avg RR (Wins): {round(avg_rr,2)}{mae_mfe}\n\n"
        f"All-Time:\n"
        f"  W: {all_wins}  BE: {all_be}  L: {all_loss}\n"
        f"  Win Rate: {round(all_wr,1)}%\n"
        f"  Expectancy: {all_exp:+.3f}R  "
        f"{'edge' if all_exp > 0 else 'no edge yet'}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# SETUP OUTCOME ANALYSIS  (Plan 28 — retroactive OHLCV level-crossing check)
#
# For every generated signal we fetch 1h OHLCV in the 24h window after
# generation and check whether entry/SL/TP1 levels were hit.
# Results are cached in signal_log (only computed once per signal).
# Called automatically before /edge to ensure fresh data.
# ──────────────────────────────────────────────────────────────────────────────

_KC_TF_SECS = 3600   # 1h in seconds


def _fetch_1h_ohlcv(pair, market_type, start_ts_ms, limit=48):
    """
    Fetch up to `limit` 1h candles starting from start_ts_ms (epoch ms).
    Returns list of (ts_ms, open, high, low, close) tuples, oldest first.
    Returns [] on any error.
    """
    try:
        if market_type == "spot":
            sym_dash = pair.replace("/", "-").split(":")[0]
            start_s  = start_ts_ms // 1000
            end_s    = start_s + limit * _KC_TF_SECS
            r = requests.get(
                "https://api.kucoin.com/api/v1/market/candles",
                params={"symbol": sym_dash, "type": "1hour",
                        "startAt": start_s, "endAt": end_s},
                timeout=12,
            )
            data = r.json().get("data") or []
            # KuCoin: newest-first → reverse; [time_s, open, close, high, low, ...]
            rows = [
                (int(c[0]) * 1000,
                 float(c[1]),   # open
                 float(c[3]),   # high
                 float(c[4]),   # low
                 float(c[2]))   # close
                for c in reversed(data)
            ]
            return [r for r in rows if r[0] >= start_ts_ms]
        else:
            sym_mx = pair.replace("/USDT:USDT", "_USDT").replace("/", "_")
            r = requests.get(
                f"https://contract.mexc.com/api/v1/contract/kline/{sym_mx}",
                params={"interval": "Min60"},
                timeout=12,
            )
            d      = r.json().get("data") or {}
            times  = d.get("time",  [])
            opens  = d.get("open",  [])
            highs  = d.get("high",  [])
            lows   = d.get("low",   [])
            closes = d.get("close", [])
            rows = [
                (int(times[i]) * 1000,
                 float(opens[i]),
                 float(highs[i]),
                 float(lows[i]),
                 float(closes[i]))
                for i in range(len(times))
                if int(times[i]) * 1000 >= start_ts_ms
            ]
            return sorted(rows, key=lambda x: x[0])[:limit]
    except Exception as e:
        print(f"[edge] OHLCV fetch error {pair}: {e}")
        return []


def _check_levels(sig, entry, sl, tp1, candles):
    """
    Walk 1h OHLCV candles and determine what happened to this setup.

    Phase 1 — scan all candles for entry level crossing (using high/low range).
    Phase 2 — from the candle where entry was hit, walk forward and find first
               level reached (SL or TP1).

    Uses a 0.3% tolerance on entry (same as _check_entry_hit in bot.py).

    Returns:
        entry_reached  bool
        tp1_reached    bool | None  (None if entry not reached)
        sl_reached     bool | None
        setup_outcome  str  'win'|'loss'|'no_entry'|'ambiguous'|'open'
    """
    entry_hit_idx = None
    for i, (ts, o, h, l, c) in enumerate(candles):
        if sig == "BUY"  and h >= entry * 0.997:
            entry_hit_idx = i
            break
        if sig == "SELL" and l <= entry * 1.003:
            entry_hit_idx = i
            break

    if entry_hit_idx is None:
        return False, None, None, "no_entry"

    # Scan from entry candle onwards for SL / TP1 crossing
    tp1_hit = False
    sl_hit  = False
    for ts, o, h, l, c in candles[entry_hit_idx:]:
        if sig == "BUY":
            if l <= sl:   sl_hit  = True
            if h >= tp1:  tp1_hit = True
        else:
            if h >= sl:   sl_hit  = True
            if l <= tp1:  tp1_hit = True
        if sl_hit or tp1_hit:
            break

    if tp1_hit and not sl_hit:
        outcome = "win"
    elif sl_hit and not tp1_hit:
        outcome = "loss"
    elif sl_hit and tp1_hit:
        outcome = "ambiguous"   # both hit in same 1h candle — can't tell order
    else:
        outcome = "open"        # entry hit but neither SL nor TP within window

    return True, tp1_hit, sl_hit, outcome


def _populate_outcomes():
    """
    Retroactively compute setup_outcome for signals that need it.

    Fetches 1h OHLCV for each unique pair (batched) and calls _check_levels.
    Results are stored in signal_log — cached permanently, only computed once.

    For FILLED signals (stage='filled'): marks setup_outcome='executed' since
    the actual trade outcome is already tracked in the trades table.
    """
    rows = get_signals_for_outcome_analysis()
    if not rows:
        return

    print(f"[edge] computing setup outcomes for {len(rows)} signal(s)…")

    # Group by (pair, market_type) to minimise OHLCV calls
    from itertools import groupby
    from operator  import itemgetter

    # Sort by pair+market_type, then process in groups
    rows_sorted = sorted(rows, key=lambda r: (r["pair"], r["market_type"]))

    for (pair, mt), group in groupby(rows_sorted,
                                     key=lambda r: (r["pair"], r["market_type"])):
        group = list(group)

        # For filled signals: no OHLCV needed — outcome = 'executed'
        filled  = [r for r in group if r.get("stage") == "filled"]
        unfilled = [r for r in group if r.get("stage") != "filled"]

        for r in filled:
            update_signal_outcome(r["id"], True, None, None, "executed")

        if not unfilled:
            continue

        # Find the earliest signal generation time for this pair in this batch
        try:
            earliest_gen = min(
                datetime.fromisoformat(str(r["generated_at"]).replace(" ", "T"))
                for r in unfilled
            )
            start_ts_ms = int(earliest_gen.timestamp() * 1000)
        except Exception as e:
            print(f"[edge] timestamp error {pair}: {e}")
            continue

        candles = _fetch_1h_ohlcv(pair, mt, start_ts_ms, limit=72)  # 72h window
        if not candles:
            continue

        time.sleep(0.15)   # gentle rate limiting between pairs

        for r in unfilled:
            try:
                gen_ts = int(
                    datetime.fromisoformat(
                        str(r["generated_at"]).replace(" ", "T")
                    ).timestamp() * 1000
                )
            except Exception:
                continue

            # Slice candles to start from this signal's generation time
            sig_candles = [c for c in candles if c[0] >= gen_ts]
            if not sig_candles:
                continue

            entry_reached, tp1_reached, sl_reached, outcome = _check_levels(
                r["signal"],
                float(r["entry"]),
                float(r["sl"]),
                float(r["tp"]),
                sig_candles,
            )
            update_signal_outcome(
                r["id"], entry_reached, tp1_reached, sl_reached, outcome
            )
            print(
                f"[edge] {pair} {r['signal']} [{r['stage']}] "
                f"→ {outcome}  entry_hit={entry_reached}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# EDGE REPORT  (Plan 28 — signal funnel + direction accuracy from signal_log)
# ──────────────────────────────────────────────────────────────────────────────

def _pct(n, d):
    return f"{n / d * 100:.0f}%" if d and d > 0 else "—"


def _dir_accuracy_block(df_with_4h, label="Direction @ 4h"):
    """Return a multi-line string showing direction accuracy for a subset."""
    n = len(df_with_4h)
    if n == 0:
        return f"{label}: no snapshots yet"
    acc = df_with_4h["dir_4h"].mean() * 100
    marker = "✓" if acc >= 55 else "✗"
    buy4  = df_with_4h[df_with_4h["signal"] == "BUY"]
    sell4 = df_with_4h[df_with_4h["signal"] == "SELL"]
    b_acc = buy4["dir_4h"].mean()  * 100 if len(buy4)  > 0 else None
    s_acc = sell4["dir_4h"].mean() * 100 if len(sell4) > 0 else None
    lines = [f"{label}  (N={n})"]
    lines.append(f"  All   {acc:.0f}% {marker}")
    if b_acc is not None:
        lines.append(f"  BUY   {b_acc:.0f}%  (N={len(buy4)})")
    if s_acc is not None:
        lines.append(f"  SELL  {s_acc:.0f}%  (N={len(sell4)})")
    return "\n".join(lines)


def _edge_full(df, title="EDGE REPORT (30d)"):
    """Full funnel + direction accuracy for a given signal_log DataFrame subset."""
    if df.empty:
        return (
            f"{title}\n{'─'*24}\n"
            f"No signals logged yet.\n"
            f"Signal tracking active from Plan 28 onwards."
        )

    total = len(df)
    sc    = df["stage"].value_counts().to_dict()

    # Stage counts:
    #   stage='queued'      → currently in pending_trades (not yet resolved)
    #   stage='filled'      → entry triggered (via queue) OR bounce (immediate fill)
    #   stage='expired'/'invalidated'/'cancelled' → resolved without fill
    #
    # Bounce signals are logged as stage='filled' + trade_type='bounce' directly.
    # All other fills start as 'queued', then transition → 'filled'.
    pending_now  = sc.get("queued",      0)   # still live in pending queue
    filled_all   = sc.get("filled",      0)   # all fills (bounce + triggered)
    expired      = sc.get("expired",     0)
    invalidated  = sc.get("invalidated", 0)
    cancelled    = sc.get("cancelled",   0)

    bounce       = len(df[(df["stage"] == "filled") & (df["trade_type"] == "bounce")])
    filled_trig  = filled_all - bounce         # filled via pending queue (entry hit)

    # Signals that went through the queue = expired + invalidated + cancelled
    #                                       + pending_now + filled_trig
    was_queued   = expired + invalidated + cancelled + pending_now + filled_trig
    q_fill_rate  = _pct(filled_trig, was_queued)

    funnel = (
        f"Signal Funnel:\n"
        f"  Placed       {total}   (all gates cleared)\n"
        f"  ├ Bounce     {bounce}  (immediate fill)\n"
        f"  └ Queued     {was_queued}  ({_pct(was_queued, total)} of placed)\n"
        f"      Filled   {filled_trig}  ({q_fill_rate} of queued)\n"
        f"      Pending  {pending_now}  (live)\n"
        f"      Expired  {expired}\n"
        f"      Invalid  {invalidated}\n"
        f"      Cancelled {cancelled}\n"
        f"  Total filled {filled_all}  ({_pct(filled_all, total)} fill rate)"
    )

    df_4h = df[df["dir_4h"].notna()]
    dir_block = _dir_accuracy_block(df_4h)

    # 24h accuracy if we have enough data
    df_24h = df[df["dir_24h"].notna()]
    if len(df_24h) >= 5:
        acc24 = df_24h["dir_24h"].mean() * 100
        dir_block += f"\n  @ 24h  {acc24:.0f}%  (N={len(df_24h)})"

    # Quick breakdown by signal direction
    dir_sep = ""
    if len(df_4h) >= 5:
        buy4  = df_4h[df_4h["signal"] == "BUY"]
        sell4 = df_4h[df_4h["signal"] == "SELL"]
        b_acc = buy4["dir_4h"].mean()  * 100 if len(buy4)  > 0 else None
        s_acc = sell4["dir_4h"].mean() * 100 if len(sell4) > 0 else None
        if b_acc is not None and s_acc is not None:
            better = "BUY" if b_acc >= s_acc else "SELL"
            dir_sep = f"\n→ {better} signals more directionally accurate"

    # ── Setup outcome section (retrospective OHLCV analysis) ──────────────────
    outcome_block = _build_outcome_block(df)

    return (
        f"{title}\n{'─'*24}\n"
        f"{funnel}\n{'─'*24}\n"
        f"{dir_block}"
        f"{dir_sep}"
        f"\n{'─'*24}\n"
        f"{outcome_block}"
    )


def _build_outcome_block(df):
    """
    Build the setup-outcome section of the edge report.

    Shows entry_reached rate + win/loss breakdown on ALL signals the bot
    generated — both filled (executed) and unfilled — so the strategy can
    see how its level choices played out regardless of execution.
    """
    if "setup_outcome" not in df.columns:
        return "Setup outcomes: no data yet"

    has_outcome = df[df["setup_outcome"].notna()]
    if len(has_outcome) == 0:
        return "Setup outcomes: computing… (run /edge again in a few minutes)"

    total_with = len(has_outcome)

    # Unfilled analysis (strategy quality independent of execution)
    unfilled = has_outcome[has_outcome["stage"] != "filled"]
    executed = has_outcome[has_outcome["stage"] == "filled"]   # outcome in /stats

    lines = [f"Setup Outcomes (N={total_with} with data)"]

    if len(unfilled) > 0:
        n_uf        = len(unfilled)
        entry_hit   = unfilled[unfilled["entry_reached"] == True]
        no_entry    = unfilled[unfilled["setup_outcome"] == "no_entry"]
        wins        = unfilled[unfilled["setup_outcome"] == "win"]
        losses      = unfilled[unfilled["setup_outcome"] == "loss"]
        ambiguous   = unfilled[unfilled["setup_outcome"] == "ambiguous"]
        open_setup  = unfilled[unfilled["setup_outcome"] == "open"]

        n_hit = len(entry_hit)
        lines.append(f"\nUnfilled signals  (N={n_uf})")
        lines.append(f"  Entry reached   {n_hit}  ({_pct(n_hit, n_uf)})")
        if n_hit > 0:
            lines.append(f"  ├ Win  (TP1 hit) {len(wins)}  ({_pct(len(wins), n_hit)})")
            lines.append(f"  ├ Loss (SL hit)  {len(losses)}  ({_pct(len(losses), n_hit)})")
            if len(ambiguous) > 0:
                lines.append(f"  ├ Ambiguous      {len(ambiguous)}")
            if len(open_setup) > 0:
                lines.append(f"  └ Unresolved     {len(open_setup)}")
        lines.append(f"  No entry ever   {len(no_entry)}  ({_pct(len(no_entry), n_uf)})")

        # The key insight: correct setup % = wins / all unfilled
        if n_uf > 0 and len(wins) > 0:
            correct_pct = len(wins) / n_uf * 100
            lines.append(
                f"\n→ {correct_pct:.0f}% of unfilled signals were winning setups "
                f"(entry + TP reached)"
            )
            if len(no_entry) / n_uf > 0.5:
                lines.append(
                    "→ >50% never triggered — entry levels too far from market?"
                )
            if n_hit > 0 and len(losses) / n_hit > 0.5:
                lines.append(
                    "→ >50% of triggered setups hit SL — SLs too tight or "
                    "setup direction wrong"
                )

    if len(executed) > 0:
        lines.append(
            f"\nExecuted ({len(executed)}) — see /stats for trade outcomes"
        )

    return "\n".join(lines)


def _edge_breakdown(df, column, label):
    """Direction accuracy @ 4h broken down by a categorical column."""
    if df.empty or column not in df.columns:
        return f"{label}\n\nNo data."

    df_4h  = df[df["dir_4h"].notna()]
    lines  = [f"{label}  @ 4h\n{'─'*24}"]
    values = sorted(df[column].dropna().unique(), key=str)

    for val in values:
        sub    = df[df[column] == val]
        sub_4h = sub[sub["dir_4h"].notna()]
        n_all  = len(sub)
        n_4h   = len(sub_4h)
        if n_4h == 0:
            lines.append(f"  {str(val):<14}  —  ({n_all} signals, no snapshots)")
            continue
        acc    = sub_4h["dir_4h"].mean() * 100
        marker = "✓" if acc >= 55 else "✗"
        lines.append(f"  {str(val):<14}  {acc:4.0f}%{marker}  (N={n_4h})")

    return "\n".join(lines)


def _edge_by_confidence(df):
    """Direction accuracy @ 4h by confidence band."""
    if df.empty or "confidence" not in df.columns:
        return "Edge by Confidence\n\nNo data."

    df_4h  = df[df["dir_4h"].notna()]
    lines  = [f"Edge by Confidence @ 4h\n{'─'*24}"]
    buckets = [("80-100", 80, 101), ("65-79", 65, 80), ("55-64", 55, 65)]
    for lbl, lo, hi in buckets:
        sub = df_4h[
            (df_4h["confidence"] >= lo) & (df_4h["confidence"] < hi)
        ]
        if len(sub) == 0:
            lines.append(f"  {lbl}    —")
            continue
        acc    = sub["dir_4h"].mean() * 100
        marker = "✓" if acc >= 55 else "✗"
        lines.append(f"  {lbl}    {acc:4.0f}%{marker}  (N={len(sub)})")

    return "\n".join(lines)


def get_edge_report(mode="edge"):
    """
    Dispatch edge report by mode string. Called from report_worker subprocess.

    Modes:
      edge             — full funnel + direction accuracy (30d)
      edge_session     — breakdown by session (premium/filtered)
      edge_regime      — breakdown by market_mode
      edge_dir         — breakdown by signal direction (BUY/SELL)
      edge_conf        — breakdown by confidence band
      edge_type        — breakdown by trade_type
      edge_pair_XXX    — pair-specific (XXX = pair string, partial match OK)
    """
    engine = get_engine()

    # Compute setup outcomes for any signals that don't have them yet.
    # Results are cached in DB — only fetches OHLCV for new signals.
    try:
        _populate_outcomes()
    except Exception as e:
        print(f"[edge] _populate_outcomes error: {e}")

    try:
        df_sig = pd.read_sql(f"SELECT * FROM {SIGNAL_LOG_TABLE}", engine)
    except Exception as e:
        return f"Edge report error (signal_log missing or inaccessible):\n{e}"

    if df_sig.empty:
        return (
            "EDGE REPORT\n\nNo signals logged yet.\n"
            "Signal tracking is active from Plan 28 onwards."
        )

    df_sig["generated_at"] = pd.to_datetime(df_sig["generated_at"], errors="coerce")
    cutoff = pd.Timestamp.utcnow() - pd.Timedelta(days=30)
    df = df_sig[df_sig["generated_at"] >= cutoff].copy()

    if mode == "edge":
        return _edge_full(df)

    elif mode == "edge_session":
        return _edge_breakdown(df, "session", "By Session")

    elif mode == "edge_regime":
        return _edge_breakdown(df, "market_mode", "By Regime")

    elif mode == "edge_dir":
        return _edge_breakdown(df, "signal", "By Direction")

    elif mode == "edge_conf":
        return _edge_by_confidence(df)

    elif mode == "edge_type":
        return _edge_breakdown(df, "trade_type", "By Trade Type")

    elif mode.startswith("edge_pair_"):
        pair_query = mode[len("edge_pair_"):]
        # Normalise both sides for flexible matching: BTC, BTCUSDT, BTC/USDT all match
        norm_q = pair_query.upper().replace("/", "").replace(":USDT", "")
        norm_s = (
            df["pair"]
            .str.upper()
            .str.replace("/", "", regex=False)
            .str.replace(":USDT", "", regex=False)
        )
        sub = df[norm_s.str.startswith(norm_q)]
        pair_label = pair_query.upper()
        total_all = len(df_sig[
            df_sig["pair"].str.upper().str.replace("/", "", regex=False)
            .str.replace(":USDT", "", regex=False)
            .str.startswith(norm_q)
        ])
        return _edge_full(
            sub,
            title=f"EDGE — {pair_label}  (30d / {total_all} all-time)"
        )

    # Unknown subcommand — fall back to full report
    return _edge_full(df)
