"""
Stats-only module — intentionally NOT imported at bot startup.

Bot lazy-imports this file once per day (daily_report) and on /stats command.
Pandas loads into the main process only then, and stays resident, which is
acceptable for once-daily use.

All hot-path DB operations (save_trade, check_trade_results, pending trades,
compounded balance, daily loss count) live in db.py without pandas.
"""
import json
import os
import re
import time
import requests
import pandas as pd
from datetime import datetime, date
from db import (get_engine, TRADES_TABLE, PENDING_TABLE, SIGNAL_LOG_TABLE,
                get_signals_for_outcome_analysis, update_signal_outcome,
                get_be_wins_without_check, record_post_be_tp1)


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
def _load_tracking_periods():
    """
    Load user-defined tracking periods from tracking.json (same directory as this file).

    Format (edit this file to add a new milestone):
        [
          {"name": "initial setup",  "from": "2026-04-01"},
          {"name": "signal overhaul","from": "2026-05-01"}
        ]

    Returns list of dicts {"name": str, "from": date} sorted oldest-first.
    Returns [] if the file is missing, empty, or malformed.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracking.json")
    try:
        with open(path) as f:
            raw = json.load(f)
        periods = []
        for p in raw:
            if not isinstance(p, dict):
                continue
            name = p.get("name", "").strip()
            frm  = p.get("from", "")
            if not name or not frm:
                continue
            try:
                dt = datetime.strptime(str(frm), "%Y-%m-%d").date()
                periods.append({"name": name, "from": dt})
            except ValueError:
                pass
        return sorted(periods, key=lambda x: x["from"])
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[stats] tracking.json error: {e}")
        return []


def _period_stats_block(sub: "pd.DataFrame", name: str,
                        date_label: str, is_current: bool) -> str:
    """Stats block for one user-defined tracking period."""
    prefix = "▸" if is_current else " "
    tag    = "  [current]" if is_current else ""

    if sub.empty:
        return f"{prefix} {name}{tag}  ({date_label})  — no trades yet"

    n     = len(sub)
    sw    = len(sub[sub["status"] == "WIN"])
    sb    = len(sub[sub["status"] == "BE_WIN"])
    sl_   = len(sub[sub["status"] == "LOSS"])
    wr    = (sw + sb) / n * 100
    rr    = sub[sub["status"] == "WIN"]["rr"].mean()             if sw > 0 else 0.0
    be_rr = (sub[sub["status"] == "BE_WIN"]["rr"] * 0.5).mean() if sb > 0 else 0.0
    exp   = _expectancy(sw, sb, sl_, rr, be_rr)
    best  = sub[sub["status"] == "WIN"]["rr"].max()              if sw > 0 else 0.0
    sample = f"\n  ⚠ early sample ({n}/30)" if n < 30 else ""

    return (
        f"{prefix} {name}{tag}  ({date_label})  "
        f"{n} trade{'s' if n != 1 else ''}\n"
        f"  W:{sw}  BE:{sb}  L:{sl_}  WR {wr:.0f}%  "
        f"Avg RR {rr:.2f}  Best {best:.2f}\n"
        f"  Expectancy  {exp:+.3f}R"
        f"{sample}"
    )


def _period_breakdown(closed_df: "pd.DataFrame", periods: list) -> str:
    """
    Return a BY PERIOD section for user-defined tracking periods.

    Each period runs from its 'from' date up to (but not including) the next
    period's 'from' date. The last period runs to today.
    Periods are listed newest-first; current (last added) is marked ▸.
    """
    if not periods or closed_df.empty or "time" not in closed_df.columns:
        return ""

    df = closed_df.copy()
    df["_date"] = pd.to_datetime(df["time"], errors="coerce").dt.date

    # Build windows oldest→newest, then reverse for display
    windows = []
    for i, p in enumerate(periods):
        end = periods[i + 1]["from"] if i + 1 < len(periods) else None
        windows.append({"name": p["name"], "start": p["from"], "end": end})

    blocks = []
    for i, w in enumerate(reversed(windows)):
        is_current = (i == 0)   # first after reversing = most recent
        mask = df["_date"] >= w["start"]
        if w["end"] is not None:
            mask &= df["_date"] < w["end"]
        sub = df[mask]

        # Human-readable date range
        if w["end"] is None:
            date_label = f"since {w['start'].strftime('%b %d, %Y')}"
        else:
            date_label = (
                f"{w['start'].strftime('%b %d')} – "
                f"{(w['end'] - __import__('datetime').timedelta(days=1)).strftime('%b %d, %Y')}"
            )

        blocks.append(_period_stats_block(sub, w["name"], date_label, is_current))

    return f"\n{'─'*22}\nBY PERIOD:\n\n" + "\n\n".join(blocks)


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

    # ── All-time headline ─────────────────────────────────────────────────────
    at_wins   = len(all_closed[all_closed["status"] == "WIN"])
    at_be     = len(all_closed[all_closed["status"] == "BE_WIN"])
    at_losses = len(all_closed[all_closed["status"] == "LOSS"])
    at_total  = at_wins + at_be + at_losses
    at_wr     = (at_wins + at_be) / at_total * 100
    at_rr     = all_closed[all_closed["status"] == "WIN"]["rr"].mean()             if at_wins > 0 else 0.0
    at_be_rr  = (all_closed[all_closed["status"] == "BE_WIN"]["rr"] * 0.5).mean() if at_be   > 0 else 0.0
    at_exp    = _expectancy(at_wins, at_be, at_losses, at_rr, at_be_rr)

    at_sorted = all_closed.copy()
    if "time" in at_sorted.columns:
        at_sorted = at_sorted.sort_values(pd.to_datetime(at_sorted["time"], errors="coerce"))
    at_results = at_sorted["status"].apply(
        lambda s: "W" if s in ("WIN", "BE_WIN") else "L"
    ).tolist()
    streak = 1 if at_results else 0
    for i in range(len(at_results) - 1, 0, -1):
        if at_results[i] == at_results[i - 1]:
            streak += 1
        else:
            break
    streak_label = (f"{streak}W" if at_results and at_results[-1] == "W"
                    else f"{streak}L" if at_results else "—")
    edge_note = "Positive edge" if at_exp > 0 else "No edge yet — keep tracking"

    header = (
        f"ALL-TIME STATS\n{'─'*22}\n"
        f"Trades   {at_total} closed  •  Streak  {streak_label}\n"
        f"W: {at_wins}  BE: {at_be}  L: {at_losses}  (WR {at_wr:.1f}%)\n"
        f"Expectancy  {at_exp:+.3f}R  ←  {edge_note}"
    )

    # ── User-defined period breakdown ─────────────────────────────────────────
    periods = _load_tracking_periods()
    period_section = _period_breakdown(all_closed, periods)

    # ── Confidence breakdown scoped to most recent period (or all-time) ───────
    conf_section = ""
    if periods and "time" in all_closed.columns:
        cur_period = periods[-1]
        conf_base  = all_closed.copy()
        conf_base["_date"] = pd.to_datetime(conf_base["time"], errors="coerce").dt.date
        conf_base  = conf_base[conf_base["_date"] >= cur_period["from"]]
        conf_label = cur_period["name"]
    else:
        conf_base  = all_closed
        conf_label = "all-time"

    if "confidence" in conf_base.columns and len(conf_base) >= 5:
        conf_df = conf_base[conf_base["confidence"].notna()].copy()
        conf_df["confidence"] = conf_df["confidence"].astype(int)
        if len(conf_df) >= 5:
            buckets = [("80-100", 80, 100), ("65-79", 65, 79),
                       ("55-64", 55, 64), ("35-54", 35, 54)]
            lines = []
            for lbl, lo, hi in buckets:
                sub = conf_df[(conf_df["confidence"] >= lo) & (conf_df["confidence"] <= hi)]
                if len(sub) == 0:
                    continue
                sw   = len(sub[sub["status"] == "WIN"])
                sb   = len(sub[sub["status"] == "BE_WIN"])
                sl   = len(sub[sub["status"] == "LOSS"])
                swr  = (sw + sb) / len(sub) * 100
                s_rr = sub[sub["status"] == "WIN"]["rr"].mean() if sw > 0 else 0.0
                s_be = (sub[sub["status"] == "BE_WIN"]["rr"] * 0.5).mean() if sb > 0 else 0.0
                s_exp = _expectancy(sw, sb, sl, s_rr, s_be)
                lines.append(f"  {lbl}  WR {swr:.0f}%  {s_exp:+.2f}R  ({len(sub)})")
            if lines:
                conf_section = (
                    f"\n{'─'*22}\n"
                    f"Confidence ({conf_label}):\n"
                    + "\n".join(lines)
                )

    return f"{header}{period_section}{conf_section}"


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
# POST-BE TP1 ANALYSIS
# For every BE_WIN trade: did price return to TP1 after the BE stop was hit?
# Phase walk: TP1 crossed → entry (BE stop) crossed → TP1 crossed again = True
# Tells us whether BE activation timing is leaving profit on the table.
# ──────────────────────────────────────────────────────────────────────────────

def _populate_post_be_outcomes():
    """
    Retroactively compute post_be_tp1 for BE_WIN trades.

    Walks 72h of 1h OHLCV in three phases:
      Phase 0 — find first TP1 crossing  (the hit that armed the BE)
      Phase 1 — find BE (entry) crossing  (the stop that closed the trade)
      Phase 2 — find TP1 crossing again   → post_be_tp1 = True

    Cached permanently in trades.post_be_tp1 — only runs for new BE_WINs.
    """
    rows = get_be_wins_without_check()
    if not rows:
        return

    print(f"[diagnose] checking post-BE TP1 for {len(rows)} trade(s)…")
    for row in rows:
        try:
            pair  = row["pair"]
            mt    = row["market_type"]
            sig   = row["signal"]
            entry = float(row["entry"])
            tp1   = float(row["tp"])

            # Fetch 72h of 1h OHLCV starting from trade open time
            try:
                gen_dt  = datetime.fromisoformat(str(row["time"]).replace(" ", "T"))
                start_ms = int(gen_dt.timestamp() * 1000)
            except Exception:
                continue

            candles = _fetch_1h_ohlcv(pair, mt, start_ms, limit=72)
            if not candles:
                record_post_be_tp1(row["time"], pair, False)
                continue

            # Phase walk
            phase  = 0   # 0=find TP1, 1=find BE, 2=find TP1 again
            result = False
            for (ts, o, h, l, c) in candles:
                if sig == "BUY":
                    if phase == 0 and h >= tp1:   phase = 1
                    elif phase == 1 and l <= entry: phase = 2
                    elif phase == 2 and h >= tp1:  result = True; break
                else:
                    if phase == 0 and l <= tp1:   phase = 1
                    elif phase == 1 and h >= entry: phase = 2
                    elif phase == 2 and l <= tp1:  result = True; break

            record_post_be_tp1(row["time"], pair, result)
            print(
                f"[diagnose] post-BE TP1 {pair} {sig}: "
                f"{'returned to TP1 ✓' if result else 'did not reach TP1 ✗'}"
            )
            time.sleep(0.1)
        except Exception as e:
            print(f"[diagnose] post_be_tp1 error {row.get('pair')}: {e}")


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


def _edge_all_pairs(df):
    """
    Rank every pair in the 30d signal_log window by direction accuracy @ 4h,
    fill rate, and setup win rate.  Flags chronic underperformers.

    Used by /edge pairs — gives a quick blacklist-or-keep decision per pair.
    """
    if df.empty or "pair" not in df.columns:
        return "No pair data available."

    pairs   = sorted(df["pair"].dropna().unique())
    lines   = [f"ALL PAIRS  (30d)\n{'─'*24}"]
    lines.append(
        f"  {'Pair':<18} {'Sigs':>4} {'Fill':>5} {'Dir@4h':>7} {'SetupW':>7}  Flag"
    )
    lines.append(f"  {'-'*18} {'-'*4} {'-'*5} {'-'*7} {'-'*7}  ----")

    flagged = []
    for pair in pairs:
        sub = df[df["pair"] == pair]
        n   = len(sub)

        # Fill rate (queued signals that filled)
        was_q   = sub[sub["stage"].isin(["expired","invalidated","cancelled","filled","queued"])]
        was_q   = was_q[was_q["trade_type"] != "bounce"]   # exclude bounce (always fills)
        filled  = was_q[was_q["stage"] == "filled"]
        fill_r  = f"{len(filled)/len(was_q)*100:.0f}%" if len(was_q) > 0 else "—"

        # Direction accuracy @ 4h
        sub_4h  = sub[sub["dir_4h"].notna()]
        if len(sub_4h) >= 3:
            acc    = sub_4h["dir_4h"].mean() * 100
            dir_s  = f"{acc:.0f}%"
            dir_ok = acc >= 50
        else:
            acc    = None
            dir_s  = f"—({len(sub_4h)})"
            dir_ok = True   # not enough data to flag

        # Setup win rate on unfilled signals that had entry hit
        unfilled = sub[(sub["stage"] != "filled") & sub["setup_outcome"].notna()]
        hit      = unfilled[unfilled["entry_reached"] == True]
        wins_u   = hit[hit["setup_outcome"] == "win"]
        if len(hit) >= 3:
            wwh   = len(wins_u) / len(hit) * 100
            swh_s = f"{wwh:.0f}%"
            swh_ok = wwh >= 40
        else:
            wwh    = None
            swh_s  = f"—({len(hit)})"
            swh_ok = True

        # Flag: poor direction AND poor setup
        flag = ""
        if acc is not None and wwh is not None:
            if acc < 45 and wwh < 35:
                flag = "⚠ BLACKLIST"
                flagged.append(pair)
            elif acc < 45 or wwh < 35:
                flag = "⚠ watch"

        lines.append(
            f"  {pair:<18} {n:>4} {fill_r:>5} {dir_s:>7} {swh_s:>7}  {flag}"
        )

    if flagged:
        lines.append(f"\n{'─'*24}")
        lines.append(f"Blacklist candidates ({len(flagged)}):")
        for p in flagged:
            lines.append(f"  {p}")
        lines.append("→ Consider adding to get_pairs() exclusion list in screener_worker.py")

    return "\n".join(lines)


def _edge_by_atr(df):
    """
    Fill rate, direction accuracy @ 4h, and setup win rate split by ATR%
    environment.  ATR% = atr / price_at_gen × 100 (both in signal_log).
    Buckets: <2% (quiet), 2-5% (normal), >5% (volatile).
    """
    if df.empty:
        return "No data for ATR breakdown."
    if "atr" not in df.columns or "price_at_gen" not in df.columns:
        return "Edge by ATR\n\nATR or price_at_gen column missing from signal_log."

    df = df.copy()
    valid = (
        df["price_at_gen"].notna() & (df["price_at_gen"] > 0) & df["atr"].notna()
    )
    df.loc[valid, "atr_pct"] = (
        df.loc[valid, "atr"] / df.loc[valid, "price_at_gen"] * 100
    )

    if df["atr_pct"].isna().all():
        return (
            "Edge by ATR\n\n"
            "ATR% could not be computed — price_at_gen may not be populated yet.\n"
            "Signals logged after Plan 28 will have this column."
        )

    buckets = [("<2%",  0,    2),
               ("2-5%", 2,    5),
               (">5%",  5, 9999)]

    lines = [f"EDGE by ATR Environment  (30d)\n{'─'*32}"]
    lines.append(
        f"  {'Bucket':<6} {'N':>4} {'Fill':>5} {'Dir@4h':>7} {'SetupW':>7}  Note"
    )
    lines.append(
        f"  {'──────':<6} {'──':>4} {'─────':>5} {'──────':>7} {'──────':>7}  ────"
    )

    warn_buckets = []
    for lbl, lo, hi in buckets:
        sub = df[(df["atr_pct"] >= lo) & (df["atr_pct"] < hi)]
        n   = len(sub)
        if n == 0:
            lines.append(f"  {lbl:<6} {n:>4}   —       —       —")
            continue

        # Fill rate (queued → triggered, bounce excluded)
        was_q  = sub[sub["stage"].isin(
            ["expired", "invalidated", "cancelled", "filled", "queued"]
        )]
        was_q  = was_q[was_q["trade_type"] != "bounce"]
        filled = was_q[was_q["stage"] == "filled"]
        fill_s = (
            f"{len(filled) / len(was_q) * 100:.0f}%"
            if len(was_q) > 0 else "—"
        )

        # Direction @ 4h
        sub_4h = sub[sub["dir_4h"].notna()]
        if len(sub_4h) >= 5:
            acc   = sub_4h["dir_4h"].mean() * 100
            dir_m = "✓" if acc >= 55 else ("⚠" if acc >= 45 else "✗")
            dir_s = f"{acc:.0f}%{dir_m}"
        else:
            acc   = None
            dir_s = f"—({len(sub_4h)})"

        # Setup win rate (unfilled signals where entry was reached)
        unfilled = sub[
            (sub["stage"] != "filled") & sub["setup_outcome"].notna()
        ]
        hit    = unfilled[unfilled["entry_reached"] == True]
        wins_u = hit[hit["setup_outcome"] == "win"]
        if len(hit) >= 3:
            wwh  = len(wins_u) / len(hit) * 100
            sw_s = f"{wwh:.0f}%"
        else:
            wwh  = None
            sw_s = f"—({len(hit)})"

        note = ""
        if acc is not None and acc < 45:
            note = "⚠ weak dir"
            warn_buckets.append(lbl)
        elif wwh is not None and wwh < 35:
            note = "⚠ weak setup"
            warn_buckets.append(lbl)

        lines.append(
            f"  {lbl:<6} {n:>4} {fill_s:>5} {dir_s:>7} {sw_s:>7}  {note}"
        )

    if warn_buckets:
        lines.append(f"\n{'─'*32}")
        if "<2%" in warn_buckets:
            lines.append("⚠ Low-ATR (<2%) signals underperform")
            lines.append(
                "  Fix: add ATR% floor in screener_worker.py momentum_score()\n"
                "  e.g. skip pairs where atr/price < 0.018 (1.8%)"
            )
        if ">5%" in warn_buckets:
            lines.append("⚠ High-ATR (>5%) signals underperform")
            lines.append(
                "  Fix: raise confidence floor in HIGH vol regime,\n"
                "  or scale position size down (conf/120 × 2% instead of /100)"
            )

    return "\n".join(lines)


def get_edge_report(mode="edge"):
    """
    Dispatch edge report by mode string. Called from report_worker subprocess.

    Modes:
      edge             — full funnel + direction accuracy (30d)
      edge_pairs       — all pairs ranked by dir accuracy + setup win rate
      edge_atr         — funnel + direction split by ATR% environment
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

    elif mode == "edge_pairs":
        return _edge_all_pairs(df)

    elif mode == "edge_atr":
        return _edge_by_atr(df)

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


# ──────────────────────────────────────────────────────────────────────────────
# DIAGNOSE REPORT  (automated diagnostic with ✓/⚠/✗ conclusions + fix hints)
#
# Designed to be pasted directly to Claude — no manual number interpretation
# needed. Covers four layers in order:
#   1. Signal funnel    — are signals triggering at all?
#   2. Direction layer  — is the predicted direction correct?
#   3. Setup layer      — when entry hits, do levels work?
#   4. Execution layer  — are live trades matching setup quality?
#
# Each ✗/⚠ issue includes a specific fix suggestion tied to bot.py / strategy.py.
# ──────────────────────────────────────────────────────────────────────────────

def get_diagnose_report():
    engine = get_engine()

    # Populate setup outcomes + post-BE TP1 checks (both cached, incremental)
    try:
        _populate_outcomes()
    except Exception as e:
        print(f"[diagnose] _populate_outcomes error: {e}")
    try:
        _populate_post_be_outcomes()
    except Exception as e:
        print(f"[diagnose] _populate_post_be_outcomes error: {e}")

    cutoff = pd.Timestamp.utcnow() - pd.Timedelta(days=30)

    try:
        df_sig = pd.read_sql(f"SELECT * FROM {SIGNAL_LOG_TABLE}", engine)
        df_sig["generated_at"] = pd.to_datetime(df_sig["generated_at"], errors="coerce")
        df = df_sig[df_sig["generated_at"] >= cutoff].copy()
    except Exception as e:
        return f"Diagnose error (signal_log): {e}"

    try:
        df_tr = pd.read_sql(f"SELECT * FROM {TRADES_TABLE}", engine)
        df_tr["time"] = pd.to_datetime(df_tr["time"], errors="coerce")
        df_tr_30 = df_tr[df_tr["time"] >= cutoff].copy()
    except Exception:
        df_tr_30 = pd.DataFrame()

    issues    = []   # (severity 0=✗ 1=⚠, text)
    strengths = []

    lines = [f"BOT DIAGNOSIS  (last 30d)\n{'─'*26}"]

    total_sig = len(df)
    if total_sig < 10:
        lines.append(f"\nSignals: {total_sig}  ⚠ need 10+ for meaningful diagnosis")
        return "\n".join(lines)

    # ── 1. Signal funnel ──────────────────────────────────────────────────────
    sc          = df["stage"].value_counts().to_dict()
    filled_all  = sc.get("filled",      0)
    expired     = sc.get("expired",     0)
    invalidated = sc.get("invalidated", 0)
    cancelled   = sc.get("cancelled",   0)
    pending_now = sc.get("queued",      0)
    bounce      = len(df[(df["stage"] == "filled") & (df["trade_type"] == "bounce")])
    filled_trig = filled_all - bounce
    was_queued  = expired + invalidated + cancelled + pending_now + filled_trig
    fill_rate   = filled_trig / was_queued * 100 if was_queued > 0 else 0.0

    resolved   = df[df["stage"].isin(["expired", "invalidated", "filled"])]
    no_entry_n = (
        len(resolved[resolved["setup_outcome"] == "no_entry"])
        if "setup_outcome" in df.columns else 0
    )
    no_entry_rate = no_entry_n / len(resolved) * 100 if len(resolved) > 0 else 0.0

    lines.append(
        f"\n[1] SIGNAL FUNNEL  (N={total_sig})\n"
        f"  Placed → queued    {was_queued}  ({was_queued/total_sig*100:.0f}%)\n"
        f"  Queued → filled    {filled_trig}  (fill rate {fill_rate:.0f}%)"
        + (f"  ✓" if fill_rate >= 20 else "  ⚠ low")
        + f"\n  Bounce fills       {bounce}\n"
        f"  Expired/invalid    {expired + invalidated}\n"
        f"  No-entry rate      {no_entry_rate:.0f}%  of resolved"
        + ("  ✓" if no_entry_rate <= 55 else "  ⚠ high")
    )

    if no_entry_rate > 65:
        issues.append((0,
            f"✗ {no_entry_rate:.0f}% of signals never trigger (entry too far)\n"
            f"   Fix: reduce entry offset distance — currently signals wait for\n"
            f"   price to come back to a level it already left. Either bring entry\n"
            f"   closer to current price, or extend pending expiry from 1h → 2h."
        ))
    elif no_entry_rate > 50:
        issues.append((1,
            f"⚠ {no_entry_rate:.0f}% of signals expire without entry\n"
            f"   Fix: extend pending expiry (trend/reversal 1h → 90 min) or\n"
            f"   reduce minimum breakout distance (0.3×ATR → 0.2×ATR)."
        ))
    elif fill_rate >= 20:
        strengths.append(f"✓ Fill rate {fill_rate:.0f}% — entry levels reachable")

    # ── 2. Direction accuracy ─────────────────────────────────────────────────
    df_4h  = df[df["dir_4h"].notna()]
    df_24h = df[df["dir_24h"].notna()]
    n_4h   = len(df_4h)

    lines.append(f"\n[2] DIRECTION ACCURACY")
    if n_4h < 15:
        lines.append(f"  @ 4h   — conclusions suppressed (need 15, have {n_4h})")
    else:
        all_acc = df_4h["dir_4h"].mean() * 100
        buy4    = df_4h[df_4h["signal"] == "BUY"]
        sell4   = df_4h[df_4h["signal"] == "SELL"]
        b_acc   = buy4["dir_4h"].mean()  * 100 if len(buy4)  >= 8 else None
        s_acc   = sell4["dir_4h"].mean() * 100 if len(sell4) >= 8 else None

        all_m = "✓" if all_acc >= 55 else ("⚠" if all_acc >= 45 else "✗")
        lines.append(f"  @ 4h   all {all_acc:.0f}%  {all_m}  (N={n_4h})")
        if b_acc is not None:
            bm = "✓" if b_acc >= 55 else ("⚠" if b_acc >= 45 else "✗")
            lines.append(f"         BUY  {b_acc:.0f}%  {bm}  (N={len(buy4)})")
        if s_acc is not None:
            sm = "✓" if s_acc >= 55 else ("⚠" if s_acc >= 45 else "✗")
            lines.append(f"         SELL {s_acc:.0f}%  {sm}  (N={len(sell4)})")
        if len(df_24h) >= 5:
            acc24 = df_24h["dir_24h"].mean() * 100
            m24   = "✓" if acc24 >= 55 else ("⚠" if acc24 >= 45 else "✗")
            lines.append(f"  @ 24h  all {acc24:.0f}%  {m24}  (N={len(df_24h)})")

        if b_acc is not None and b_acc < 45:
            issues.append((0,
                f"✗ BUY direction accuracy {b_acc:.0f}% — signals predicting wrong direction\n"
                f"   Fix: tighten HTF bias for BUY: raise DI+ scored factors needed\n"
                f"   from 2/4 → 3/4 in recovery, or raise BUY confidence floor to 65\n"
                f"   globally. Check if 4h EMA50/200 condition is working."
            ))
        elif b_acc is not None and b_acc < 55:
            issues.append((1,
                f"⚠ BUY direction marginal ({b_acc:.0f}%) — filter weak contexts\n"
                f"   Fix: run /edge session and /edge regime to find which\n"
                f"   session/market_mode drives the bad calls, then disable BUY there."
            ))
        elif b_acc is not None:
            strengths.append(f"✓ BUY direction healthy ({b_acc:.0f}%) — maintain BUY logic")

        if s_acc is not None and s_acc < 45:
            issues.append((0,
                f"✗ SELL direction accuracy {s_acc:.0f}% — weak short signal quality\n"
                f"   Fix: raise SELL HTF bias to 4/4 (normal vol), 3/4 (high vol).\n"
                f"   Consider SELL signals only in bear mode or confirmed downtrend."
            ))
        elif s_acc is not None and s_acc < 55:
            issues.append((1,
                f"⚠ SELL direction marginal ({s_acc:.0f}%)\n"
                f"   Fix: run /edge regime — SELL may only work in bear mode.\n"
                f"   Raise SELL confidence floor to 70 in normal/recovery mode."
            ))
        elif s_acc is not None:
            strengths.append(f"✓ SELL direction healthy ({s_acc:.0f}%) — maintain SELL logic")

    # ── ATR environment split ─────────────────────────────────────────────────
    if "atr" in df.columns and "price_at_gen" in df.columns:
        df_a = df.copy()
        v = df_a["price_at_gen"].notna() & (df_a["price_at_gen"] > 0) & df_a["atr"].notna()
        df_a.loc[v, "atr_pct"] = df_a.loc[v, "atr"] / df_a.loc[v, "price_at_gen"] * 100
        n_computed = df_a["atr_pct"].notna().sum()
        if n_computed >= 5:
            low_atr  = df_a[df_a["atr_pct"] <  2]
            mid_atr  = df_a[(df_a["atr_pct"] >= 2) & (df_a["atr_pct"] < 5)]
            high_atr = df_a[df_a["atr_pct"] >= 5]
            lines.append(f"\n[ATR] ENVIRONMENT SPLIT")
            for lbl, sub in [("<2%", low_atr), ("2-5%", mid_atr), (">5%", high_atr)]:
                sub_4h = sub[sub["dir_4h"].notna()]
                if len(sub_4h) >= 5:
                    acc = sub_4h["dir_4h"].mean() * 100
                    m   = "✓" if acc >= 55 else ("⚠" if acc >= 45 else "✗")
                    lines.append(
                        f"  ATR {lbl:<6}  N={len(sub):>3}  dir@4h {acc:>3.0f}%  {m}"
                    )
                else:
                    lines.append(
                        f"  ATR {lbl:<6}  N={len(sub):>3}  dir@4h —  "
                        f"(need 5 snapshots, have {len(sub_4h)})"
                    )
            # Flag weak ATR buckets (N>=8 snapshots required)
            for lbl, sub in [("<2%", low_atr), (">5%", high_atr)]:
                sub_4h = sub[sub["dir_4h"].notna()]
                if len(sub_4h) >= 8:
                    acc = sub_4h["dir_4h"].mean() * 100
                    if acc < 45:
                        if lbl == "<2%":
                            fix = (
                                "add ATR% floor in screener_worker.py momentum_score()\n"
                                "   e.g. skip pairs where atr/price < 0.018 (1.8%)"
                            )
                        else:
                            fix = (
                                "raise confidence floor in HIGH vol regime, or\n"
                                "   reduce position size (conf/120 × 2% instead of /100)"
                            )
                        issues.append((1,
                            f"⚠ ATR {lbl} signals: direction accuracy {acc:.0f}%\n"
                            f"   Fix: {fix}"
                        ))

    # ── 3. Setup quality (unfilled signal OHLCV analysis) ─────────────────────
    lines.append(f"\n[3] SETUP QUALITY  (unfilled signals)")
    if "setup_outcome" in df.columns:
        unfilled = df[(df["stage"] != "filled") & df["setup_outcome"].notna()]
        if len(unfilled) < 10:
            lines.append(f"  conclusions suppressed (need 10 unfilled with outcomes, have {len(unfilled)})")
        else:
            entry_hit  = unfilled[unfilled["entry_reached"] == True]
            wins_uf    = unfilled[unfilled["setup_outcome"] == "win"]
            losses_uf  = unfilled[unfilled["setup_outcome"] == "loss"]
            n_hit      = len(entry_hit)
            hit_rate   = n_hit / len(unfilled) * 100
            wwh        = len(wins_uf) / n_hit * 100 if n_hit > 0 else 0.0

            hit_m = "✓" if hit_rate >= 30 else "⚠"
            wwh_m = "✓" if wwh >= 55 else ("⚠" if wwh >= 40 else "✗")
            lines.append(
                f"  Entry hit rate     {hit_rate:.0f}%  ({n_hit}/{len(unfilled)})  {hit_m}\n"
                f"  Win when hit       {wwh:.0f}%  (W:{len(wins_uf)} L:{len(losses_uf)})  {wwh_m}"
            )

            if wwh < 35 and n_hit >= 8:
                issues.append((0,
                    f"✗ Setup win rate only {wwh:.0f}% when entry is triggered\n"
                    f"   Possible causes:\n"
                    f"   a) SL too tight — check 0.5×ATR minimum is applying to all\n"
                    f"      entry types (especially pullback/bounce)\n"
                    f"   b) TP1 too ambitious — 2.0×ATR cap may not be tight enough\n"
                    f"      for current volatility regime\n"
                    f"   c) Signal direction wrong — cross-check with direction accuracy above"
                ))
            elif wwh < 55 and n_hit >= 8:
                issues.append((1,
                    f"⚠ Setup win rate marginal ({wwh:.0f}% when entry hit)\n"
                    f"   Fix: raise global confidence floor from 55 → 60 to filter\n"
                    f"   weaker setups. Also check if loss rate varies by trade_type\n"
                    f"   (/edge type) — one type may be dragging the average down."
                ))
            elif n_hit >= 8:
                strengths.append(
                    f"✓ Setup quality good ({wwh:.0f}% win when entry hit) — "
                    f"signal generation logic is sound"
                )
    else:
        lines.append("  setup_outcome column missing — signal_log may be pre-Plan 28")

    # ── 4. Execution layer ────────────────────────────────────────────────────
    lines.append(f"\n[4] EXECUTION  (live closed trades, 30d)")
    closed_30 = (
        df_tr_30[df_tr_30["status"].isin(["WIN", "BE_WIN", "LOSS"])]
        if not df_tr_30.empty else pd.DataFrame()
    )
    if len(closed_30) < 10:
        lines.append(f"  conclusions suppressed (need 10, have {len(closed_30)})")
    else:
        lw    = len(closed_30[closed_30["status"] == "WIN"])
        lb    = len(closed_30[closed_30["status"] == "BE_WIN"])
        ll    = len(closed_30[closed_30["status"] == "LOSS"])
        lwr   = (lw + lb) / len(closed_30) * 100
        l_rr  = closed_30[closed_30["status"] == "WIN"]["rr"].mean()             if lw > 0 else 0.0
        l_be  = (closed_30[closed_30["status"] == "BE_WIN"]["rr"] * 0.5).mean() if lb > 0 else 0.0
        l_exp = _expectancy(lw, lb, ll, l_rr, l_be)

        lwr_m = "✓" if lwr >= 50 else ("⚠" if lwr >= 40 else "✗")
        exp_m = "✓" if l_exp > 0 else "✗"
        lines.append(
            f"  Win rate     {lwr:.0f}%  (W:{lw} BE:{lb} L:{ll})  {lwr_m}\n"
            f"  Avg RR       {l_rr:.2f}\n"
            f"  Expectancy   {l_exp:+.3f}R  {exp_m}"
        )

        # Post-BE TP1 check — did BE_WIN trades leave profit on the table?
        if lb >= 5 and "post_be_tp1" in closed_30.columns:
            be_df      = closed_30[closed_30["status"] == "BE_WIN"]
            checked    = be_df[be_df["post_be_tp1"].notna()]
            continued  = checked[checked["post_be_tp1"] == True]
            if len(checked) >= 5:
                cont_pct = len(continued) / len(checked) * 100
                cont_m   = "⚠" if cont_pct >= 50 else "✓"
                lines.append(
                    f"\n  Post-BE TP1 hit  {cont_pct:.0f}%  "
                    f"({len(continued)}/{len(checked)} BE_WINs)  {cont_m}"
                )
                if cont_pct >= 60:
                    issues.append((1,
                        f"⚠ {cont_pct:.0f}% of BE_WIN trades reached TP1 after the BE stop\n"
                        f"   Trades are being stopped out and then price continues to target.\n"
                        f"   Fix: delay BE activation from 1:1 → 1.3:1 to give the runner\n"
                        f"   more room before locking in breakeven."
                    ))
                elif cont_pct >= 50:
                    issues.append((1,
                        f"⚠ {cont_pct:.0f}% of BE_WINs hit TP1 after exit — marginal\n"
                        f"   Consider testing BE activation at 1.2:1 via backtest."
                    ))
                else:
                    strengths.append(
                        f"✓ Post-BE TP1 rate {cont_pct:.0f}% — BE activation timing is correct"
                    )

        # BE_WIN heavy → trail activation timing issue (when post_be_tp1 not yet populated)
        elif lb > 0 and lb / len(closed_30) > 0.35:
            issues.append((1,
                f"⚠ High BE_WIN rate ({lb}/{len(closed_30)} = {lb/len(closed_30)*100:.0f}%)\n"
                f"   Run /diagnose again after 48h — post-BE TP1 analysis will populate\n"
                f"   and give a specific fix recommendation."
            ))

        if l_exp <= 0:
            issues.append((0,
                f"✗ Negative expectancy on live trades ({l_exp:+.3f}R)\n"
                f"   If setup quality (layer 3) is ✓, the problem is execution:\n"
                f"   - Late entry guard may be letting stale entries through\n"
                f"   - Check if SL is being placed correctly at time of fill\n"
                f"   - Verify trail_sl activation isn't triggering prematurely"
            ))
        else:
            strengths.append(f"✓ Live expectancy positive ({l_exp:+.3f}R) — execution working")

    # ── Summary ───────────────────────────────────────────────────────────────
    lines.append(f"\n{'─'*26}\nSUMMARY")

    critical = [t for s, t in issues if s == 0]
    warnings = [t for s, t in issues if s == 1]

    if not critical and not warnings and not strengths:
        lines.append("Not enough data yet for conclusions.")
    else:
        if critical:
            lines.append("\nCRITICAL (fix first):")
            for t in critical:
                lines.append(f"\n{t}")
        if warnings:
            lines.append("\nWARNINGS:")
            for t in warnings:
                lines.append(f"\n{t}")
        if strengths:
            lines.append("\nWORKING:")
            for t in strengths:
                lines.append(f"  {t}")

    return "\n".join(lines)
