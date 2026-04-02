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
                be_activated BOOLEAN, trail_sl FLOAT
            )
        """))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {PENDING_TABLE} (
                pair TEXT, signal TEXT, entry FLOAT, sl FLOAT,
                tp FLOAT, rr FLOAT, market_type TEXT,
                trade_type TEXT, atr FLOAT, queued_at TEXT
            )
        """))
        conn.commit()


# ==============================
# SAVE TRADE
# ==============================
def save_trade(pair, signal, entry, sl, tp, rr, market_type, atr=0.0):
    ensure_csv()
    engine = get_engine()
    row = pd.DataFrame([{
        "time": str(datetime.utcnow()),
        "pair": pair, "signal": signal,
        "entry": round(entry, 8), "sl": round(sl, 8),
        "tp": round(tp, 8), "rr": rr,
        "status": "OPEN", "market_type": market_type,
        "atr": round(float(atr), 8),
        "be_activated": False, "trail_sl": round(sl, 8)
    }])
    row.to_sql(TRADES_TABLE, engine, if_exists="append", index=False)


# ==============================
# PENDING TRADE PERSISTENCE
# ==============================
def save_pending_trades(pending_trades):
    engine = get_engine()
    rows = []
    for t in pending_trades:
        rows.append({
            "pair": t["pair"],
            "signal": t["signal"],
            "entry": t["entry"],
            "sl": t["sl"],
            "tp": t["tp"],
            "rr": t["rr"],
            "market_type": t["market_type"],
            "trade_type": t.get("trade_type", "trend"),
            "atr": float(t.get("atr", 0.0)),
            "queued_at": t["time"].isoformat(),
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
        trades.append({
            "pair": row["pair"],
            "signal": row["signal"],
            "entry": float(row["entry"]),
            "sl": float(row["sl"]),
            "tp": float(row["tp"]),
            "rr": row["rr"],
            "market_type": row["market_type"],
            "trade_type": row["trade_type"],
            "atr": float(row["atr"]),
            "time": queued_at,
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
# TP/SL + BREAKEVEN + TRAILING
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

        print(f"Checking {row['pair']} | Price: {price:.6f}")

        entry = float(row['entry'])
        sl = float(row['sl'])
        tp = float(row['tp'])
        trail_sl = float(row['trail_sl']) if not pd.isna(row['trail_sl']) else sl
        be_activated = bool(row['be_activated'])
        risk = abs(entry - sl)
        sig = row['signal']
        changes = {}

        pair = row['pair']
        direction = "LONG" if sig == "BUY" else "SHORT"

        if sig == "BUY":
            if not be_activated and price >= entry + risk:
                changes['trail_sl'] = entry
                changes['be_activated'] = True
                trail_sl = entry
                be_activated = True
                send_telegram(
                    f"🔒 BREAKEVEN SET\n"
                    f"{pair}  {direction}\n"
                    f"SL moved to entry @ {_fmt(entry)}"
                )

            elif be_activated and price >= entry + 2 * risk:
                new_trail = price - (1.2 * risk)
                if new_trail > trail_sl:
                    changes['trail_sl'] = round(new_trail, 8)
                    trail_sl = new_trail

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
            elif price >= tp:
                changes['status'] = "WIN"
                pnl_pct = abs(tp - entry) / entry * 100
                send_telegram(
                    f"✅ TAKE PROFIT HIT\n"
                    f"{pair}  {direction}\n"
                    f"Exit @ {_fmt(tp)}  (+{pnl_pct:.2f}%)  RR 1:{row['rr']}"
                )

        elif sig == "SELL":
            if not be_activated and price <= entry - risk:
                changes['trail_sl'] = entry
                changes['be_activated'] = True
                trail_sl = entry
                be_activated = True
                send_telegram(
                    f"🔒 BREAKEVEN SET\n"
                    f"{pair}  {direction}\n"
                    f"SL moved to entry @ {_fmt(entry)}"
                )

            elif be_activated and price <= entry - 2 * risk:
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
            elif price <= tp:
                changes['status'] = "WIN"
                pnl_pct = abs(tp - entry) / entry * 100
                send_telegram(
                    f"✅ TAKE PROFIT HIT\n"
                    f"{pair}  {direction}\n"
                    f"Exit @ {_fmt(tp)}  (+{pnl_pct:.2f}%)  RR 1:{row['rr']}"
                )

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

    msg = f"""
📊 DAILY REPORT ({today})

Today:
  Open: {open_t} | Closed: {closed}
  W: {wins}  BE: {be_wins}  L: {losses}
  Win Rate: {round(winrate, 1)}%
  Avg RR (Wins): {round(avg_rr, 2)}

All-Time:
  W: {all_wins}  BE: {all_be}  L: {all_losses}
  Win Rate: {round(all_wr, 1)}%
"""
    send_telegram(msg)
