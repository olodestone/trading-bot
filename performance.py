import pandas as pd
import os
from datetime import datetime
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL", "")


def _fmt(p):
    if p >= 1000:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:.4f}"
    elif p >= 0.01:
        return f"{p:.6f}"
    elif p >= 0.0001:
        return f"{p:.8f}"
    else:
        return f"{p:.10f}"

TRADES_TABLE = "trades"
PENDING_TABLE = "pending_trades"

COLUMNS = [
    "time", "pair", "signal", "entry", "sl", "tp", "rr",
    "status", "market_type", "atr", "be_activated", "trail_sl"
]


def get_engine():
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if not url:
        raise RuntimeError("DATABASE_URL env var is not set")
    return create_engine(url)


# ==============================
# ENSURE TABLES EXIST
# ==============================
def ensure_csv():
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {TRADES_TABLE} (
                time TEXT, pair TEXT, signal TEXT,
                entry FLOAT, sl FLOAT, tp FLOAT, rr FLOAT,
                status TEXT, market_type TEXT, atr FLOAT,
                be_activated BOOLEAN, trail_sl FLOAT,
                tp2 FLOAT, tp1_hit BOOLEAN
            )
        """))
        # Add new columns to existing tables without breaking live data
        for col, typedef in [("tp2", "FLOAT"), ("tp1_hit", "BOOLEAN"), ("risk_dollars", "FLOAT"),
                             ("mae", "FLOAT"), ("mfe", "FLOAT"),
                             ("time_to_mfe", "FLOAT"), ("time_to_mae", "FLOAT"),
                             ("confidence", "INTEGER")]:
            try:
                conn.execute(text(
                    f"ALTER TABLE {TRADES_TABLE} ADD COLUMN IF NOT EXISTS {col} {typedef}"
                ))
            except Exception:
                pass
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {PENDING_TABLE} (
                pair TEXT, signal TEXT, entry FLOAT, sl FLOAT,
                tp FLOAT, tp2 FLOAT, rr FLOAT, market_type TEXT,
                trade_type TEXT, atr FLOAT, queued_at TEXT
            )
        """))
        for col, typedef in [("tp2", "FLOAT"), ("confidence", "INTEGER")]:
            try:
                conn.execute(text(
                    f"ALTER TABLE {PENDING_TABLE} ADD COLUMN IF NOT EXISTS {col} {typedef}"
                ))
            except Exception:
                pass
        conn.commit()


# ==============================
# SAVE TRADE
# ==============================
def save_trade(pair, signal, entry, sl, tp, tp2, rr, market_type, atr=0.0, risk_dollars=0.0, confidence=50):
    ensure_csv()
    engine = get_engine()
    row = pd.DataFrame([{
        "time": str(datetime.utcnow()),
        "pair": pair, "signal": signal,
        "entry": round(entry, 8), "sl": round(sl, 8),
        "tp": round(tp, 8),
        "tp2": round(tp2, 8) if tp2 is not None else None,
        "rr": rr,
        "status": "OPEN", "market_type": market_type,
        "atr": round(float(atr), 8),
        "be_activated": False, "trail_sl": round(sl, 8),
        "tp1_hit": False,
        "risk_dollars": round(float(risk_dollars), 4),
        "confidence": int(confidence),
    }])
    row.to_sql(TRADES_TABLE, engine, if_exists="append", index=False)


# ==============================
# PENDING TRADE PERSISTENCE
# ==============================
def save_pending_trades(pending_trades):
    engine = get_engine()
    rows = []
    for t in pending_trades:
        tp2 = t.get("tp2")
        rows.append({
            "pair": t["pair"],
            "signal": t["signal"],
            "entry": t["entry"],
            "sl": t["sl"],
            "tp": t["tp"],
            "tp2": round(tp2, 8) if tp2 is not None else None,
            "rr": t["rr"],
            "market_type": t["market_type"],
            "trade_type": t.get("trade_type", "trend"),
            "atr": float(t.get("atr", 0.0)),
            "queued_at": t["time"].isoformat(),
            "confidence": int(t.get("confidence", 3)),
        })
    with engine.connect() as conn:
        conn.execute(text(f"DELETE FROM {PENDING_TABLE}"))
        conn.commit()
    if rows:
        pd.DataFrame(rows).to_sql(PENDING_TABLE, engine, if_exists="append", index=False)


def load_pending_trades():
    engine = get_engine()
    try:
        df = pd.read_sql(f"SELECT * FROM {PENDING_TABLE}", engine)
    except Exception:
        return []
    if df.empty:
        return []
    trades = []
    for _, row in df.iterrows():
        try:
            queued_at = datetime.fromisoformat(row["queued_at"])
        except Exception:
            queued_at = datetime.utcnow()
        raw_tp2 = row.get("tp2")
        tp2 = float(raw_tp2) if raw_tp2 is not None and not pd.isna(raw_tp2) else None
        raw_conf = row.get("confidence")
        trades.append({
            "pair": row["pair"],
            "signal": row["signal"],
            "entry": float(row["entry"]),
            "sl": float(row["sl"]),
            "tp": float(row["tp"]),
            "tp2": tp2,
            "rr": row["rr"],
            "market_type": row["market_type"],
            "trade_type": row["trade_type"],
            "atr": float(row["atr"]),
            "time": queued_at,
            "confidence": int(raw_conf) if raw_conf is not None and not pd.isna(raw_conf) else 50,
        })
    return trades


# ==============================
# DAILY LOSS COUNT
# ==============================
def get_daily_losses():
    engine = get_engine()
    try:
        df = pd.read_sql(f"SELECT time, status FROM {TRADES_TABLE}", engine)
        df['time'] = pd.to_datetime(df['time'], errors='coerce')
        today = datetime.utcnow().date()
        return len(df[(df['time'].dt.date == today) & (df['status'] == 'LOSS')])
    except Exception:
        return 0


# ==============================
# COMPOUNDING
# ==============================
def get_compounded_balance(starting_balance):
    """
    Theoretical account balance = starting capital + cumulative closed trade P&L.

    P&L approximation per trade:
      WIN             → rr × risk_dollars          (full target reached)
      BE_WIN+tp1_hit  → 0.5 × rr × risk_dollars   (TP1 partial, rest at breakeven)
      BE_WIN          → 0                           (breakeven, no P&L)
      LOSS            → −risk_dollars

    Rows without risk_dollars (pre-migration) are skipped to avoid bad data.
    Balance is floored at $1 so sizing never breaks.
    """
    try:
        engine = get_engine()
        df = pd.read_sql(
            f"SELECT status, rr, risk_dollars, tp1_hit FROM {TRADES_TABLE} WHERE status != 'OPEN'",
            engine
        )
    except Exception:
        return starting_balance

    if df.empty:
        return starting_balance

    pnl = 0.0
    for _, row in df.iterrows():
        risk_raw = row.get('risk_dollars')
        if risk_raw is None or pd.isna(risk_raw):
            continue
        risk = float(risk_raw)
        if risk <= 0:
            continue
        rr_raw = row.get('rr')
        rr   = float(rr_raw) if rr_raw is not None and not pd.isna(rr_raw) else 0.0
        tp1  = bool(row['tp1_hit']) if row.get('tp1_hit') is not None and not pd.isna(row.get('tp1_hit', float('nan'))) else False
        if row['status'] == 'WIN':
            pnl += rr * risk
        elif row['status'] == 'BE_WIN':
            pnl += 0.5 * rr * risk if tp1 else 0.0
        elif row['status'] == 'LOSS':
            pnl -= risk

    if pd.isna(pnl):
        pnl = 0.0
    return round(max(starting_balance + pnl, 1.0), 4)


# ==============================
# TP/SL + BREAKEVEN + TRAILING + TIERED TP
# ==============================
def check_trade_results(fetch_price_func, send_telegram):
    ensure_csv()
    engine = get_engine()
    df = pd.read_sql(f"SELECT * FROM {TRADES_TABLE} WHERE status = 'OPEN'", engine)
    if df.empty:
        return

    updates = []  # list of (row_time, row_pair, changes_dict)

    for _, row in df.iterrows():
        try:
            price = fetch_price_func(row['pair'], row['market_type'])
        except Exception as e:
            print(f"Price fetch error: {row['pair']} -> {e}")
            continue

        if price is None:
            continue
        price = float(price)

        print(f"Checking {row['pair']} | Price: {price:.6f}")

        entry = float(row['entry'])
        sl = float(row['sl'])
        tp1 = float(row['tp'])
        trail_sl = float(row['trail_sl']) if not pd.isna(row['trail_sl']) else sl
        be_activated = bool(row['be_activated'])
        risk = abs(entry - sl)
        sig = row['signal']
        changes = {}

        raw_tp2 = row.get('tp2')
        tp2 = float(raw_tp2) if raw_tp2 is not None and not pd.isna(raw_tp2) else None
        raw_tp1_hit = row.get('tp1_hit')
        tp1_hit = bool(raw_tp1_hit) if raw_tp1_hit is not None and not pd.isna(raw_tp1_hit) else False

        pair = row['pair']
        direction = "LONG" if sig == "BUY" else "SHORT"

        # MAE/MFE: update on every scan, overwrite when new max
        try:
            entry_time = pd.to_datetime(row['time']).replace(tzinfo=None)
            hours_elapsed = round((datetime.utcnow() - entry_time).total_seconds() / 3600, 2)
        except Exception:
            hours_elapsed = 0.0
        existing_mfe = float(row['mfe']) if row.get('mfe') is not None and not pd.isna(row.get('mfe', float('nan'))) else 0.0
        existing_mae = float(row['mae']) if row.get('mae') is not None and not pd.isna(row.get('mae', float('nan'))) else 0.0
        if sig == "BUY":
            cur_mfe = max((price - entry) / risk, 0.0)
            cur_mae = max((entry - price) / risk, 0.0)
        else:
            cur_mfe = max((entry - price) / risk, 0.0)
            cur_mae = max((price - entry) / risk, 0.0)
        if cur_mfe > existing_mfe:
            changes['mfe'] = round(cur_mfe, 3)
            changes['time_to_mfe'] = hours_elapsed
        if cur_mae > existing_mae:
            changes['mae'] = round(cur_mae, 3)
            changes['time_to_mae'] = hours_elapsed

        if sig == "BUY":
            # 1:1 hit → move SL to breakeven
            if not be_activated and not tp1_hit and price >= entry + risk:
                changes['be_activated'] = True
                changes['trail_sl'] = entry
                be_activated = True
                trail_sl = entry
                send_telegram(
                    f"🔒 1:1 HIT — SL → Breakeven\n"
                    f"{'─' * 22}\n"
                    f"{pair}  {direction}\n"
                    f"Entry @ {_fmt(entry)}  |  SL moved to entry\n"
                    f"TP1 @ {_fmt(tp1)}  still live"
                )

            # Trail: tighten SL at 2:1+ (only runs after BE is activated)
            if be_activated and price >= entry + 2 * risk:
                new_trail = price - (1.2 * risk)
                if new_trail > trail_sl:
                    changes['trail_sl'] = round(new_trail, 8)
                    trail_sl = new_trail

            # Stopped out
            if price <= trail_sl:
                changes['status'] = "BE_WIN" if be_activated else "LOSS"
                if be_activated:
                    send_telegram(
                        f"🔒 TRAIL STOP CLOSED\n"
                        f"{pair}  {direction}\n"
                        f"Exit @ {_fmt(price)}  (protected by BE)"
                    )
                else:
                    send_telegram(
                        f"❌ STOP LOSS HIT\n"
                        f"{pair}  {direction}\n"
                        f"Exit @ {_fmt(price)}"
                    )
            # TP2 hit (runner target — tp1 already taken)
            elif tp1_hit and tp2 and price >= tp2:
                changes['status'] = "WIN"
                tp2_pct = abs(tp2 - entry) / entry * 100
                send_telegram(
                    f"🏆 TP2 HIT\n"
                    f"{'─' * 22}\n"
                    f"{pair}  {direction}\n"
                    f"Close 25% @ {_fmt(tp2)}  (+{tp2_pct:.2f}%)\n"
                    f"RR 1:{row['rr']}  Trail the rest"
                )
            # TP1 hit — close 50%, move SL to entry (BE set here, not at 1:1)
            elif not tp1_hit and price >= tp1:
                changes['tp1_hit'] = True
                changes['be_activated'] = True
                changes['trail_sl'] = entry
                tp1_hit = True
                be_activated = True
                trail_sl = entry
                tp1_pct = abs(tp1 - entry) / entry * 100
                tp2_line = f"TP2 @ {_fmt(tp2)}" if tp2 else "No TP2 — trail remainder"
                send_telegram(
                    f"🎯 TP1 HIT — Close 50%\n"
                    f"{'─' * 22}\n"
                    f"{pair}  {direction}\n"
                    f"Exit 50% @ {_fmt(tp1)}  (+{tp1_pct:.2f}%)\n"
                    f"{tp2_line}\n"
                    f"SL → entry (BE set)"
                )
                # If no TP2, close the full trade as WIN
                if not tp2:
                    changes['status'] = "WIN"

        elif sig == "SELL":
            # 1:1 hit → move SL to breakeven
            if not be_activated and not tp1_hit and price <= entry - risk:
                changes['be_activated'] = True
                changes['trail_sl'] = entry
                be_activated = True
                trail_sl = entry
                send_telegram(
                    f"🔒 1:1 HIT — SL → Breakeven\n"
                    f"{'─' * 22}\n"
                    f"{pair}  {direction}\n"
                    f"Entry @ {_fmt(entry)}  |  SL moved to entry\n"
                    f"TP1 @ {_fmt(tp1)}  still live"
                )

            # Trail: tighten SL at 2:1+ (only runs after BE is activated)
            if be_activated and price <= entry - 2 * risk:
                new_trail = price + (1.2 * risk)
                if new_trail < trail_sl:
                    changes['trail_sl'] = round(new_trail, 8)
                    trail_sl = new_trail

            if price >= trail_sl:
                changes['status'] = "BE_WIN" if be_activated else "LOSS"
                if be_activated:
                    send_telegram(
                        f"🔒 TRAIL STOP CLOSED\n"
                        f"{pair}  {direction}\n"
                        f"Exit @ {_fmt(price)}  (protected by BE)"
                    )
                else:
                    send_telegram(
                        f"❌ STOP LOSS HIT\n"
                        f"{pair}  {direction}\n"
                        f"Exit @ {_fmt(price)}"
                    )
            elif tp1_hit and tp2 and price <= tp2:
                changes['status'] = "WIN"
                tp2_pct = abs(tp2 - entry) / entry * 100
                send_telegram(
                    f"🏆 TP2 HIT\n"
                    f"{'─' * 22}\n"
                    f"{pair}  {direction}\n"
                    f"Close 25% @ {_fmt(tp2)}  (+{tp2_pct:.2f}%)\n"
                    f"RR 1:{row['rr']}  Trail the rest"
                )
            # TP1 hit — close 50%, move SL to entry (BE set here, not at 1:1)
            elif not tp1_hit and price <= tp1:
                changes['tp1_hit'] = True
                changes['be_activated'] = True
                changes['trail_sl'] = entry
                tp1_hit = True
                be_activated = True
                trail_sl = entry
                tp1_pct = abs(tp1 - entry) / entry * 100
                tp2_line = f"TP2 @ {_fmt(tp2)}" if tp2 else "No TP2 — trail remainder"
                send_telegram(
                    f"🎯 TP1 HIT — Close 50%\n"
                    f"{'─' * 22}\n"
                    f"{pair}  {direction}\n"
                    f"Exit 50% @ {_fmt(tp1)}  (+{tp1_pct:.2f}%)\n"
                    f"{tp2_line}\n"
                    f"SL → entry (BE set)"
                )
                if not tp2:
                    changes['status'] = "WIN"

        if changes:
            updates.append((str(row['time']), row['pair'], changes))

    if updates:
        with engine.connect() as conn:
            for row_time, row_pair, changes in updates:
                set_clause = ", ".join(f"{k} = :{k}" for k in changes)
                conn.execute(
                    text(f"UPDATE {TRADES_TABLE} SET {set_clause} WHERE time = :t AND pair = :p"),
                    {**changes, "t": row_time, "p": row_pair}
                )
            conn.commit()


# ==============================
# EXPECTANCY HELPER
# ==============================
def _expectancy(wins, be_wins, losses, avg_win_rr, avg_be_rr=0.0):
    """
    True expectancy per trade risked (in R).
    WIN: earns avg_win_rr. BE_WIN: earns 0.5×TP1_rr (partial close, rest at entry).
    LOSS: costs 1R.
    """
    total = wins + be_wins + losses
    if total == 0:
        return 0.0
    return round((wins * avg_win_rr + be_wins * avg_be_rr - losses * 1.0) / total, 3)


# ==============================
# STATS SUMMARY (for /stats command)
# ==============================
def get_stats_summary():
    """Returns a formatted string with all-time performance stats."""
    try:
        engine = get_engine()
        df = pd.read_sql(f"SELECT * FROM {TRADES_TABLE}", engine)
    except Exception as e:
        return f"Stats error: {e}"

    if df.empty:
        return "📈 STATS\n\nNo closed trades yet. Keep watching signals."

    wins = len(df[df['status'] == "WIN"])
    be_wins = len(df[df['status'] == "BE_WIN"])
    losses = len(df[df['status'] == "LOSS"])
    total_closed = wins + be_wins + losses

    if total_closed == 0:
        return "📈 STATS\n\nNo closed trades yet."

    win_rate = (wins + be_wins) / total_closed * 100
    avg_rr = df[df['status'] == "WIN"]['rr'].mean() if wins > 0 else 0.0
    best_rr = df[df['status'] == "WIN"]['rr'].max() if wins > 0 else 0.0
    avg_be_rr = (df[df['status'] == "BE_WIN"]['rr'] * 0.5).mean() if be_wins > 0 else 0.0
    expectancy = _expectancy(wins, be_wins, losses, avg_rr, avg_be_rr)

    # Streak calculation
    closed_df = df[df['status'].isin(["WIN", "BE_WIN", "LOSS"])].copy()
    closed_df['time'] = pd.to_datetime(closed_df['time'], errors='coerce')
    closed_df = closed_df.sort_values('time')
    results = closed_df['status'].apply(lambda s: "W" if s in ("WIN", "BE_WIN") else "L").tolist()

    cur_streak = 1 if results else 0
    for i in range(len(results) - 1, 0, -1):
        if results[i] == results[i - 1]:
            cur_streak += 1
        else:
            break
    streak_label = f"{cur_streak}W" if results and results[-1] in ("W",) else f"{cur_streak}L" if results else "—"

    edge_note = "✅ Positive edge" if expectancy > 0 else "⚠️ No edge yet — keep tracking"

    # Confidence breakdown — only shown once enough trades exist
    conf_section = ""
    if 'confidence' in df.columns:
        conf_df = df[df['status'].isin(["WIN", "BE_WIN", "LOSS"]) & df['confidence'].notna()].copy()
        conf_df['confidence'] = conf_df['confidence'].astype(int)
        if len(conf_df) >= 5:
            buckets = [("80-100", 80, 100), ("65-79", 65, 79),
                       ("50-64", 50, 64), ("35-49", 35, 49), ("0-34", 0, 34)]
            conf_lines = []
            for label, lo, hi in buckets:
                sub = conf_df[(conf_df['confidence'] >= lo) & (conf_df['confidence'] <= hi)]
                if len(sub) == 0:
                    continue
                s_wins   = len(sub[sub['status'] == "WIN"])
                s_be     = len(sub[sub['status'] == "BE_WIN"])
                s_losses = len(sub[sub['status'] == "LOSS"])
                s_wr     = (s_wins + s_be) / len(sub) * 100
                s_avg_rr = sub[sub['status'] == "WIN"]['rr'].mean() if s_wins > 0 else 0.0
                s_avg_be = (sub[sub['status'] == "BE_WIN"]['rr'] * 0.5).mean() if s_be > 0 else 0.0
                s_exp    = _expectancy(s_wins, s_be, s_losses, s_avg_rr, s_avg_be)
                conf_lines.append(f"  {label}  WR {s_wr:.0f}%  {s_exp:+.2f}R  ({len(sub)})")
            if conf_lines:
                conf_section = f"\n{'─' * 22}\nBy confidence:\n" + "\n".join(conf_lines)

    return (
        f"📈 ALL-TIME STATS\n"
        f"{'─' * 22}\n"
        f"Trades   {total_closed} closed\n"
        f"W: {wins}  BE: {be_wins}  L: {losses}\n"
        f"Win Rate {win_rate:.1f}%\n"
        f"Avg RR   {avg_rr:.2f}\n"
        f"Best RR  {best_rr:.2f}\n"
        f"{'─' * 22}\n"
        f"Expectancy  {expectancy:+.3f}R\n"
        f"{edge_note}\n"
        f"Streak   {streak_label}\n"
        f"{'─' * 22}\n"
        f"ℹ️ Need 30+ trades for reliable stats."
        f"{conf_section}"
    )


# ==============================
# MAE/MFE DIAGNOSTIC SECTION
# ==============================
def _mae_mfe_section(df_closed):
    if df_closed.empty or 'mfe' not in df_closed.columns:
        return ""
    df = df_closed.dropna(subset=['mfe', 'mae'])
    if df.empty:
        return ""

    wins   = df[df['status'] == "WIN"]
    be     = df[df['status'] == "BE_WIN"]
    losses = df[df['status'] == "LOSS"]

    lines = ["\nMAE/MFE:"]

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
            f"peak @{_ttf(be, 'time_to_mfe'):.1f}h  ← exits too early?"
        )
    if not losses.empty:
        lines.append(
            f"  Loss ({len(losses)}):  avg MFE {losses['mfe'].mean():.2f}R  "
            f"avg MAE {losses['mae'].mean():.2f}R  "
            f"SL hit @{_ttf(losses, 'time_to_mae'):.1f}h"
        )

    return "\n".join(lines) if len(lines) > 1 else ""


# ==============================
# DAILY REPORT
# ==============================
def daily_report(send_telegram):
    ensure_csv()
    engine = get_engine()
    df = pd.read_sql(f"SELECT * FROM {TRADES_TABLE}", engine)

    if df.empty:
        send_telegram("📊 DAILY REPORT\n\nNo trades yet.")
        return

    df['time'] = pd.to_datetime(df['time'], errors='coerce')
    today = datetime.utcnow().date()
    df_today = df[df['time'].dt.date == today]

    wins = len(df_today[df_today['status'] == "WIN"])
    be_wins = len(df_today[df_today['status'] == "BE_WIN"])
    losses = len(df_today[df_today['status'] == "LOSS"])
    open_t = len(df_today[df_today['status'] == "OPEN"])
    closed = wins + be_wins + losses
    winrate = ((wins + be_wins) / closed * 100) if closed > 0 else 0

    avg_rr = df_today[df_today['status'] == "WIN"]['rr'].mean() if wins > 0 else 0.0

    all_wins = len(df[df['status'] == "WIN"])
    all_be = len(df[df['status'] == "BE_WIN"])
    all_losses = len(df[df['status'] == "LOSS"])
    all_closed = all_wins + all_be + all_losses
    all_wr = ((all_wins + all_be) / all_closed * 100) if all_closed > 0 else 0
    all_avg_rr = df[df['status'] == "WIN"]['rr'].mean() if all_wins > 0 else 0.0
    all_avg_be_rr = (df[df['status'] == "BE_WIN"]['rr'] * 0.5).mean() if all_be > 0 else 0.0
    all_expectancy = _expectancy(all_wins, all_be, all_losses, all_avg_rr, all_avg_be_rr)

    df_closed_today = df_today[df_today['status'].isin(["WIN", "BE_WIN", "LOSS"])]
    mae_mfe = _mae_mfe_section(df_closed_today)

    msg = f"""
📊 DAILY REPORT ({today})

Today:
  Open: {open_t} | Closed: {closed}
  W: {wins}  BE: {be_wins}  L: {losses}
  Win Rate: {round(winrate, 1)}%
  Avg RR (Wins): {round(avg_rr, 2)}{mae_mfe}

All-Time:
  W: {all_wins}  BE: {all_be}  L: {all_losses}
  Win Rate: {round(all_wr, 1)}%
  Expectancy: {all_expectancy:+.3f}R  {"✅ edge" if all_expectancy > 0 else "❌ no edge yet"}
"""
    send_telegram(msg)
