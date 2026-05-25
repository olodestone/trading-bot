"""
Lightweight DB layer — psycopg2 only (no sqlalchemy, no pandas).

sqlalchemy cost: ~19 MB RSS in the main process.
psycopg2 cost:   ~10 MB RSS.
Switching saves ~9 MB of permanent main-process idle RAM.

All hot-path operations live here. Stats functions that need pandas are in
performance.py and are lazy-imported only for /stats and the daily report.
"""
import os
from datetime import datetime

import psycopg2
import psycopg2.extras

DATABASE_URL      = os.getenv("DATABASE_URL", "")
TRADES_TABLE      = "trades"
PENDING_TABLE     = "pending_trades"
SIGNAL_LOG_TABLE  = "signal_log"


def _conn():
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if not url:
        raise RuntimeError("DATABASE_URL env var is not set")
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


# Keep a thin sqlalchemy-compatible get_engine() shim so performance.py
# (which uses pd.read_sql) still works without changes.
def get_engine():
    from sqlalchemy import create_engine
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return create_engine(url)


def _fmt(p):
    if p >= 1000:   return f"{p:,.2f}"
    if p >= 1:      return f"{p:.4f}"
    if p >= 0.01:   return f"{p:.6f}"
    if p >= 0.0001: return f"{p:.8f}"
    return f"{p:.10f}"


# ──────────────────────────────────────────────────────────────────────────────
# TABLE SETUP
# ──────────────────────────────────────────────────────────────────────────────
def ensure_tables():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {TRADES_TABLE} (
                    time TEXT, pair TEXT, signal TEXT,
                    entry FLOAT, sl FLOAT, tp FLOAT, rr FLOAT,
                    status TEXT, market_type TEXT, atr FLOAT,
                    be_activated BOOLEAN, trail_sl FLOAT,
                    tp2 FLOAT, tp1_hit BOOLEAN
                )
            """)
            for col, typedef in [
                ("tp2", "FLOAT"), ("tp1_hit", "BOOLEAN"), ("risk_dollars", "FLOAT"),
                ("mae", "FLOAT"), ("mfe", "FLOAT"),
                ("time_to_mfe", "FLOAT"), ("time_to_mae", "FLOAT"),
                ("confidence", "INTEGER"),
                ("plan", "INTEGER"),
                # post_be_tp1: did price return to TP1 after the BE stop was hit?
                # NULL = not yet checked, True/False = result of retroactive OHLCV check.
                ("post_be_tp1", "BOOLEAN"),
            ]:
                try:
                    cur.execute(
                        f"ALTER TABLE {TRADES_TABLE} "
                        f"ADD COLUMN IF NOT EXISTS {col} {typedef}"
                    )
                except Exception:
                    pass
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {PENDING_TABLE} (
                    pair TEXT, signal TEXT, entry FLOAT, sl FLOAT,
                    tp FLOAT, tp2 FLOAT, rr FLOAT, market_type TEXT,
                    trade_type TEXT, atr FLOAT, queued_at TEXT, confidence INTEGER
                )
            """)
            for col, typedef in [
                ("tp2",           "FLOAT"),
                ("confidence",    "INTEGER"),
                ("signal_log_id", "INTEGER"),
            ]:
                try:
                    cur.execute(
                        f"ALTER TABLE {PENDING_TABLE} "
                        f"ADD COLUMN IF NOT EXISTS {col} {typedef}"
                    )
                except Exception:
                    pass
        conn.commit()


# ──────────────────────────────────────────────────────────────────────────────
# TRADE CRUD
# ──────────────────────────────────────────────────────────────────────────────
def save_trade(pair, signal, entry, sl, tp, tp2, rr, market_type,
               atr=0.0, risk_dollars=0.0, confidence=50, plan=None):
    ensure_tables()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                INSERT INTO {TRADES_TABLE}
                  (time, pair, signal, entry, sl, tp, tp2, rr, status,
                   market_type, atr, be_activated, trail_sl, tp1_hit,
                   risk_dollars, confidence, plan)
                VALUES
                  (%(time)s, %(pair)s, %(signal)s, %(entry)s, %(sl)s,
                   %(tp)s, %(tp2)s, %(rr)s, 'OPEN',
                   %(market_type)s, %(atr)s, false, %(sl)s, false,
                   %(risk_dollars)s, %(confidence)s, %(plan)s)
            """, {
                "time":         str(datetime.utcnow()),
                "pair":         pair,
                "signal":       signal,
                "entry":        round(entry, 8),
                "sl":           round(sl, 8),
                "tp":           round(tp, 8),
                "tp2":          round(tp2, 8) if tp2 is not None else None,
                "rr":           rr,
                "market_type":  market_type,
                "atr":          round(float(atr), 8),
                "risk_dollars": round(float(risk_dollars), 4),
                "confidence":   int(confidence),
                "plan":         int(plan) if plan is not None else None,
            })
        conn.commit()


# ──────────────────────────────────────────────────────────────────────────────
# PENDING TRADES
# ──────────────────────────────────────────────────────────────────────────────
def save_pending_trades(pending_trades):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {PENDING_TABLE}")
            for t in pending_trades:
                tp2 = t.get("tp2")
                cur.execute(f"""
                    INSERT INTO {PENDING_TABLE}
                      (pair, signal, entry, sl, tp, tp2, rr, market_type,
                       trade_type, atr, queued_at, confidence, signal_log_id)
                    VALUES
                      (%(pair)s, %(signal)s, %(entry)s, %(sl)s, %(tp)s,
                       %(tp2)s, %(rr)s, %(market_type)s,
                       %(trade_type)s, %(atr)s, %(queued_at)s, %(confidence)s,
                       %(signal_log_id)s)
                """, {
                    "pair":          t["pair"],
                    "signal":        t["signal"],
                    "entry":         t["entry"],
                    "sl":            t["sl"],
                    "tp":            t["tp"],
                    "tp2":           round(tp2, 8) if tp2 is not None else None,
                    "rr":            t["rr"],
                    "market_type":   t["market_type"],
                    "trade_type":    t.get("trade_type", "trend"),
                    "atr":           float(t.get("atr", 0.0)),
                    "queued_at":     t["time"].isoformat(),
                    "confidence":    int(t.get("confidence", 50)),
                    "signal_log_id": t.get("signal_log_id"),
                })
        conn.commit()


def load_pending_trades():
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM {PENDING_TABLE}")
                rows = cur.fetchall()
    except Exception:
        return []

    trades = []
    for row in rows:
        row = dict(row)
        try:
            queued_at = datetime.fromisoformat(row["queued_at"])
        except Exception:
            queued_at = datetime.utcnow()
        tp2_raw  = row.get("tp2")
        conf_raw = row.get("confidence")
        slid_raw = row.get("signal_log_id")
        trades.append({
            "pair":          row["pair"],
            "signal":        row["signal"],
            "entry":         float(row["entry"]),
            "sl":            float(row["sl"]),
            "tp":            float(row["tp"]),
            "tp2":           float(tp2_raw) if tp2_raw is not None else None,
            "rr":            row["rr"],
            "market_type":   row["market_type"],
            "trade_type":    row["trade_type"],
            "atr":           float(row["atr"]),
            "time":          queued_at,
            "confidence":    int(conf_raw) if conf_raw is not None else 50,
            "signal_log_id": int(slid_raw) if slid_raw is not None else None,
        })
    return trades


# ──────────────────────────────────────────────────────────────────────────────
# COUNTERS / GUARDS
# ──────────────────────────────────────────────────────────────────────────────
def get_daily_losses():
    today = datetime.utcnow().date().isoformat()
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM {TRADES_TABLE} "
                    f"WHERE status = 'LOSS' AND time >= %(today)s",
                    {"today": today}
                )
                row = cur.fetchone()
                return int(list(row.values())[0]) if row else 0
    except Exception:
        return 0


def get_compounded_balance(starting_balance):
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT status, rr, risk_dollars, tp1_hit FROM {TRADES_TABLE} "
                    f"WHERE status != 'OPEN'"
                )
                rows = cur.fetchall()
    except Exception:
        return starting_balance

    pnl = 0.0
    for row in rows:
        row = dict(row)
        risk_raw = row.get("risk_dollars")
        if risk_raw is None:
            continue
        risk = float(risk_raw)
        if risk <= 0:
            continue
        rr   = float(row["rr"]) if row.get("rr") is not None else 0.0
        tp1  = bool(row["tp1_hit"]) if row.get("tp1_hit") is not None else False
        if row["status"] == "WIN":
            pnl += rr * risk
        elif row["status"] == "BE_WIN":
            pnl += 0.5 * rr * risk if tp1 else 0.0
        elif row["status"] == "LOSS":
            pnl -= risk

    return round(max(starting_balance + pnl, 1.0), 4)


# ──────────────────────────────────────────────────────────────────────────────
# TRADE MONITORING
# ──────────────────────────────────────────────────────────────────────────────
def check_trade_results(get_price_func, send_telegram):
    ensure_tables()

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {TRADES_TABLE} WHERE status = 'OPEN'")
            rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return

    updates = []

    for row in rows:
        try:
            price = get_price_func(row["pair"], row["market_type"])
        except Exception as e:
            print(f"Price fetch error: {row['pair']} -> {e}")
            continue
        if price is None:
            continue
        price = float(price)

        print(f"Checking {row['pair']} | Price: {price:.6f}")

        entry    = float(row["entry"])
        sl       = float(row["sl"])
        tp1      = float(row["tp"])
        tp2_raw  = row.get("tp2")
        tp2      = float(tp2_raw) if tp2_raw is not None else None
        tsl_raw  = row.get("trail_sl")
        trail_sl = float(tsl_raw) if tsl_raw is not None else sl
        be_activated = bool(row.get("be_activated") or False)
        tp1h_raw = row.get("tp1_hit")
        tp1_hit  = bool(tp1h_raw) if tp1h_raw is not None else False
        risk     = abs(entry - sl)
        sig      = row["signal"]
        pair     = row["pair"]
        direction = "LONG" if sig == "BUY" else "SHORT"
        changes  = {}

        # MAE / MFE tracking
        try:
            entry_time    = datetime.fromisoformat(str(row.get("time", "")).replace(" ", "T"))
            hours_elapsed = round(
                (datetime.utcnow() - entry_time.replace(tzinfo=None)).total_seconds() / 3600, 2
            )
        except Exception:
            hours_elapsed = 0.0

        existing_mfe = float(row["mfe"]) if row.get("mfe") is not None else 0.0
        existing_mae = float(row["mae"]) if row.get("mae") is not None else 0.0
        if risk > 0:
            if sig == "BUY":
                cur_mfe = max((price - entry) / risk, 0.0)
                cur_mae = max((entry - price) / risk, 0.0)
            else:
                cur_mfe = max((entry - price) / risk, 0.0)
                cur_mae = max((price - entry) / risk, 0.0)
            if cur_mfe > existing_mfe:
                changes["mfe"] = round(cur_mfe, 3)
                changes["time_to_mfe"] = hours_elapsed
            if cur_mae > existing_mae:
                changes["mae"] = round(cur_mae, 3)
                changes["time_to_mae"] = hours_elapsed

        if sig == "BUY":
            if not be_activated and not tp1_hit and price >= entry + risk:
                changes.update({"be_activated": True, "trail_sl": entry})
                be_activated = True; trail_sl = entry
                send_telegram(
                    f"1:1 HIT — SL → Breakeven\n{'─'*22}\n"
                    f"{pair}  {direction}\n"
                    f"Entry @ {_fmt(entry)}  |  SL moved to entry\n"
                    f"TP1 @ {_fmt(tp1)}  still live"
                )
            if be_activated and price >= entry + 2 * risk:
                new_trail = price - 1.2 * risk
                if new_trail > trail_sl:
                    changes["trail_sl"] = round(new_trail, 8); trail_sl = new_trail
            if price <= trail_sl:
                changes["status"] = "BE_WIN" if be_activated else "LOSS"
                send_telegram(
                    f"{'TRAIL STOP CLOSED' if be_activated else 'STOP LOSS HIT'}\n"
                    f"{pair}  {direction}\nExit @ {_fmt(price)}"
                    + ("  (protected by BE)" if be_activated else "")
                )
            elif tp1_hit and tp2 and price >= tp2:
                changes["status"] = "WIN"
                send_telegram(
                    f"TP2 HIT\n{'─'*22}\n{pair}  {direction}\n"
                    f"Close 25% @ {_fmt(tp2)}  (+{abs(tp2-entry)/entry*100:.2f}%)\n"
                    f"RR 1:{row['rr']}  Trail the rest"
                )
            elif not tp1_hit and price >= tp1:
                changes.update({"tp1_hit": True, "be_activated": True, "trail_sl": entry})
                tp1_hit = be_activated = True; trail_sl = entry
                tp2_line = f"TP2 @ {_fmt(tp2)}" if tp2 else "No TP2 — trail remainder"
                send_telegram(
                    f"TP1 HIT — Close 50%\n{'─'*22}\n{pair}  {direction}\n"
                    f"Exit 50% @ {_fmt(tp1)}  (+{abs(tp1-entry)/entry*100:.2f}%)\n"
                    f"{tp2_line}\nSL → entry (BE set)"
                )
                if not tp2:
                    changes["status"] = "WIN"

        elif sig == "SELL":
            if not be_activated and not tp1_hit and price <= entry - risk:
                changes.update({"be_activated": True, "trail_sl": entry})
                be_activated = True; trail_sl = entry
                send_telegram(
                    f"1:1 HIT — SL → Breakeven\n{'─'*22}\n"
                    f"{pair}  {direction}\n"
                    f"Entry @ {_fmt(entry)}  |  SL moved to entry\n"
                    f"TP1 @ {_fmt(tp1)}  still live"
                )
            if be_activated and price <= entry - 2 * risk:
                new_trail = price + 1.2 * risk
                if new_trail < trail_sl:
                    changes["trail_sl"] = round(new_trail, 8); trail_sl = new_trail
            if price >= trail_sl:
                changes["status"] = "BE_WIN" if be_activated else "LOSS"
                send_telegram(
                    f"{'TRAIL STOP CLOSED' if be_activated else 'STOP LOSS HIT'}\n"
                    f"{pair}  {direction}\nExit @ {_fmt(price)}"
                    + ("  (protected by BE)" if be_activated else "")
                )
            elif tp1_hit and tp2 and price <= tp2:
                changes["status"] = "WIN"
                send_telegram(
                    f"TP2 HIT\n{'─'*22}\n{pair}  {direction}\n"
                    f"Close 25% @ {_fmt(tp2)}  (+{abs(tp2-entry)/entry*100:.2f}%)\n"
                    f"RR 1:{row['rr']}  Trail the rest"
                )
            elif not tp1_hit and price <= tp1:
                changes.update({"tp1_hit": True, "be_activated": True, "trail_sl": entry})
                tp1_hit = be_activated = True; trail_sl = entry
                tp2_line = f"TP2 @ {_fmt(tp2)}" if tp2 else "No TP2 — trail remainder"
                send_telegram(
                    f"TP1 HIT — Close 50%\n{'─'*22}\n{pair}  {direction}\n"
                    f"Exit 50% @ {_fmt(tp1)}  (+{abs(tp1-entry)/entry*100:.2f}%)\n"
                    f"{tp2_line}\nSL → entry (BE set)"
                )
                if not tp2:
                    changes["status"] = "WIN"

        if changes:
            updates.append((str(row["time"]), row["pair"], changes))

    if updates:
        with _conn() as conn:
            with conn.cursor() as cur:
                for row_time, row_pair, chg in updates:
                    set_clause = ", ".join(f"{k} = %({k})s" for k in chg)
                    cur.execute(
                        f"UPDATE {TRADES_TABLE} SET {set_clause} "
                        f"WHERE time = %(t)s AND pair = %(p)s",
                        {**chg, "t": row_time, "p": row_pair}
                    )
            conn.commit()


# ──────────────────────────────────────────────────────────────────────────────
# SIGNAL LOG  (Plan 28 — full funnel tracking + direction accuracy)
#
# stage values:
#   queued      — added to pending_trades (non-bounce signals)
#   filled      — entry triggered (queued→filled) OR bounce (immediate fill)
#   expired     — pending signal timed out before entry
#   invalidated — breakout failed / late-entry guard / breakdown invalidated
#   cancelled   — user /cancel'd the signal
#
# direction accuracy: dir_4h / dir_24h — was price higher (BUY) or lower (SELL)
# than price_at_gen after 4h / 24h?  True = correct call.
#
# setup outcome (computed retroactively from OHLCV in report_worker):
#   entry_reached  — did price reach the entry level (any candle's high/low)?
#   tp1_reached    — did TP1 get hit after entry (before SL)?
#   sl_reached     — did SL get hit after entry?
#   setup_outcome  — 'win'|'loss'|'no_entry'|'ambiguous'|'open'|'executed'
#                    'executed' = trade was filled (outcome tracked in trades table)
#
# trade_time links back to trades.time for filled signals (informational; no FK
# because trades has no PK — query by pair + approximate timestamp if needed).
# ──────────────────────────────────────────────────────────────────────────────
def ensure_signal_log():
    """Create signal_log table and indexes; add any missing columns via ALTER."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {SIGNAL_LOG_TABLE} (
                    id            SERIAL PRIMARY KEY,
                    generated_at  TEXT NOT NULL,
                    pair          TEXT,
                    signal        TEXT,
                    trade_type    TEXT,
                    entry         FLOAT,
                    sl            FLOAT,
                    tp            FLOAT,
                    tp2           FLOAT,
                    rr            FLOAT,
                    atr           FLOAT,
                    confidence    INTEGER,
                    market_type   TEXT,
                    session       TEXT,
                    market_mode   TEXT,
                    btc_downtrend BOOLEAN,
                    stage         TEXT DEFAULT 'queued',
                    price_at_gen  FLOAT,
                    snap_4h       FLOAT,
                    dir_4h        BOOLEAN,
                    snap_24h      FLOAT,
                    dir_24h       BOOLEAN,
                    trade_time    TEXT,
                    entry_reached BOOLEAN,
                    tp1_reached   BOOLEAN,
                    sl_reached    BOOLEAN,
                    setup_outcome TEXT
                )
            """)
            # Migrate existing tables: add new columns if missing
            for col, typedef in [
                ("entry_reached", "BOOLEAN"),
                ("tp1_reached",   "BOOLEAN"),
                ("sl_reached",    "BOOLEAN"),
                ("setup_outcome", "TEXT"),
            ]:
                try:
                    cur.execute(
                        f"ALTER TABLE {SIGNAL_LOG_TABLE} "
                        f"ADD COLUMN IF NOT EXISTS {col} {typedef}"
                    )
                except Exception:
                    pass
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_siglog_gen "
                f"ON {SIGNAL_LOG_TABLE}(generated_at)"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS idx_siglog_pair "
                f"ON {SIGNAL_LOG_TABLE}(pair)"
            )
        conn.commit()


def log_signal(pair, signal, trade_type, entry, sl, tp, tp2, rr, atr,
               confidence, market_type, session, market_mode, btc_downtrend,
               stage="queued", price_at_gen=None, trade_time=None):
    """
    Insert a signal into signal_log. Returns the new row id (int) or None.

    stage='queued'  for signals added to pending_trades.
    stage='filled'  for bounce signals (immediate fill, never queued).
    """
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {SIGNAL_LOG_TABLE}
                      (generated_at, pair, signal, trade_type,
                       entry, sl, tp, tp2, rr, atr, confidence,
                       market_type, session, market_mode, btc_downtrend,
                       stage, price_at_gen, trade_time)
                    VALUES
                      (%(t)s, %(pair)s, %(sig)s, %(tt)s,
                       %(entry)s, %(sl)s, %(tp)s, %(tp2)s,
                       %(rr)s, %(atr)s, %(conf)s,
                       %(mt)s, %(sess)s, %(mode)s, %(btc)s,
                       %(stage)s, %(pgen)s, %(ttime)s)
                    RETURNING id
                """, {
                    "t":     str(datetime.utcnow()),
                    "pair":  pair,
                    "sig":   signal,
                    "tt":    trade_type,
                    "entry": round(entry, 8),
                    "sl":    round(sl, 8),
                    "tp":    round(tp, 8),
                    "tp2":   round(tp2, 8) if tp2 is not None else None,
                    "rr":    float(rr),
                    "atr":   float(atr),
                    "conf":  int(confidence),
                    "mt":    market_type,
                    "sess":  session,
                    "mode":  market_mode,
                    "btc":   bool(btc_downtrend) if btc_downtrend is not None else None,
                    "stage": stage,
                    "pgen":  round(float(price_at_gen), 8) if price_at_gen else None,
                    "ttime": trade_time,
                })
                row = cur.fetchone()
            conn.commit()
        return dict(row)["id"] if row else None
    except Exception as e:
        print(f"log_signal error: {e}")
        return None


def update_signal_stage(signal_log_id, stage, trade_time=None):
    """Update stage (and optionally trade_time) for a logged signal."""
    if signal_log_id is None:
        return
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                if trade_time:
                    cur.execute(
                        f"UPDATE {SIGNAL_LOG_TABLE} "
                        f"SET stage=%s, trade_time=%s WHERE id=%s",
                        (stage, str(trade_time), int(signal_log_id))
                    )
                else:
                    cur.execute(
                        f"UPDATE {SIGNAL_LOG_TABLE} SET stage=%s WHERE id=%s",
                        (stage, int(signal_log_id))
                    )
            conn.commit()
    except Exception as e:
        print(f"update_signal_stage error: {e}")


def get_signals_needing_snapshots():
    """
    Return signals where 4h or 24h price snapshots are still outstanding.
    Uses Python-computed ISO strings for TEXT timestamp comparisons (safe
    across all PostgreSQL versions — avoids implicit TEXT→TIMESTAMPTZ casts).
    """
    from datetime import timedelta
    cutoff_4h  = (datetime.utcnow() - timedelta(hours=4)).isoformat()
    cutoff_24h = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT id, pair, signal, market_type, generated_at,
                           price_at_gen, snap_4h, snap_24h
                    FROM   {SIGNAL_LOG_TABLE}
                    WHERE  price_at_gen IS NOT NULL
                      AND  (
                               (snap_4h  IS NULL AND generated_at <= %(c4h)s)
                            OR (snap_24h IS NULL AND generated_at <= %(c24h)s)
                           )
                    ORDER  BY generated_at
                    LIMIT  30
                """, {"c4h": cutoff_4h, "c24h": cutoff_24h})
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"get_signals_needing_snapshots error: {e}")
        return []


def record_price_snapshot(signal_log_id, column, price, sig, price_at_gen):
    """
    Record a price snapshot and compute direction correctness.

    column  — 'snap_4h' or 'snap_24h'
    sig     — 'BUY' or 'SELL'
    direction_correct: price moved toward TP (up for BUY, down for SELL)
    """
    dir_col = column.replace("snap_", "dir_")
    direction_correct = (
        (float(price) > float(price_at_gen))
        if sig == "BUY"
        else (float(price) < float(price_at_gen))
    )
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {SIGNAL_LOG_TABLE} "
                    f"SET {column}=%s, {dir_col}=%s WHERE id=%s",
                    (round(float(price), 8), direction_correct, int(signal_log_id))
                )
            conn.commit()
    except Exception as e:
        print(f"record_price_snapshot error: {e}")


def get_signals_for_outcome_analysis():
    """
    Return signals older than 25h where setup_outcome has not been computed yet.

    25h gives a full 24h window after generation plus 1h buffer.
    Only fetches unfilled signals (stage != 'filled') — filled signals get their
    outcome from the trades table directly (via update_signal_outcome with
    setup_outcome='executed').  Limit 20 per call to bound OHLCV fetch count.
    """
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=25)).isoformat()
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT id, pair, signal, market_type, generated_at,
                           entry, sl, tp, tp2, stage, trade_time
                    FROM   {SIGNAL_LOG_TABLE}
                    WHERE  setup_outcome IS NULL
                      AND  generated_at <= %(cutoff)s
                    ORDER  BY generated_at
                    LIMIT  20
                """, {"cutoff": cutoff})
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"get_signals_for_outcome_analysis error: {e}")
        return []


def get_be_wins_without_check():
    """
    Return BE_WIN trades where post_be_tp1 has not been computed yet.
    Returns rows with: time, pair, signal, entry, tp, market_type
    Only fetches trades older than 48h (needs a 48h OHLCV window after close).
    """
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=48)).isoformat()
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT time, pair, signal, entry, tp, market_type "
                    f"FROM {TRADES_TABLE} "
                    f"WHERE status = 'BE_WIN' "
                    f"  AND post_be_tp1 IS NULL "
                    f"  AND time <= %(cutoff)s "
                    f"ORDER BY time "
                    f"LIMIT 20",
                    {"cutoff": cutoff}
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"get_be_wins_without_check error: {e}")
        return []


def record_post_be_tp1(trade_time, pair, value):
    """Store the retroactive post-BE TP1 result for a trade (True/False)."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {TRADES_TABLE} "
                    f"SET post_be_tp1 = %s "
                    f"WHERE time = %s AND pair = %s",
                    (bool(value), str(trade_time), pair)
                )
            conn.commit()
    except Exception as e:
        print(f"record_post_be_tp1 error: {e}")


def update_signal_outcome(signal_log_id,
                          entry_reached, tp1_reached, sl_reached, setup_outcome):
    """
    Store the retroactively computed setup outcome for a signal_log row.

    entry_reached  — True if price hit the entry level in the 24h OHLCV window
    tp1_reached    — True if TP1 was reached after entry (None if no entry)
    sl_reached     — True if SL was reached after entry   (None if no entry)
    setup_outcome  — 'win'|'loss'|'no_entry'|'ambiguous'|'open'|'executed'
    """
    if signal_log_id is None:
        return
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {SIGNAL_LOG_TABLE} "
                    f"SET entry_reached=%s, tp1_reached=%s, "
                    f"    sl_reached=%s, setup_outcome=%s "
                    f"WHERE id=%s",
                    (
                        bool(entry_reached) if entry_reached is not None else None,
                        bool(tp1_reached)   if tp1_reached  is not None else None,
                        bool(sl_reached)    if sl_reached   is not None else None,
                        setup_outcome,
                        int(signal_log_id),
                    )
                )
            conn.commit()
    except Exception as e:
        print(f"update_signal_outcome error: {e}")
