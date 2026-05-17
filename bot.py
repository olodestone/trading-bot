"""
Main bot process — deliberately import-light.

Heavy libraries (numpy, pandas, strategy) are loaded ONLY inside the
screener_worker subprocess. ccxt is gone from the worker too (replaced by
direct HTTP). This process idles at ~33 MB RSS between scans.

Library loads in this file:
  startup : psycopg2 (via db.py) — lightweight, one-time (~10 MB)
  per scan: subprocess spawned — numpy/pandas/strategy loaded and freed
  daily   : pandas (via performance.py) — lazy-imported for daily_report / /stats
"""
import gc
import json
import os
import signal as signal_module
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
import requests

from db import (
    TRADES_TABLE, PENDING_TABLE,
    ensure_tables,
    save_trade, load_pending_trades, save_pending_trades,
    get_daily_losses, get_compounded_balance, check_trade_results,
)
from logger import send_telegram, TOKEN, CHAT_ID


# ──────────────────────────────────────────────────────────────────────────────
# RSS HELPER
# ──────────────────────────────────────────────────────────────────────────────
def _rss_kb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        return 0


# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────
ACCOUNT_BALANCE  = float(os.getenv("ACCOUNT_BALANCE", "15"))
STARTING_BALANCE = ACCOUNT_BALANCE
RISK_PCT         = float(os.getenv("RISK_PCT", "0.02"))

MAX_CONCURRENT   = 10
MAX_DAILY_LOSSES = 5
BOT_PLAN         = 23   # increment when deploying a new plan — used to tag trades in DB

WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screener_worker.py")


# ──────────────────────────────────────────────────────────────────────────────
# MODULE STATE
# ──────────────────────────────────────────────────────────────────────────────
pending_trades       = []
last_signals         = {}   # "pair_signal" → datetime — dedup cooldown

_bear_mode_scans     = 0
_recovery_scans      = 0
_market_mode         = "normal"
_btc_downtrend       = False
_btc_downtrend_prev  = False


# ──────────────────────────────────────────────────────────────────────────────
# RAW SQL HELPERS
# ──────────────────────────────────────────────────────────────────────────────
def _db_conn():
    url = os.getenv("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def _fetchone(sql, params=None):
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            row = cur.fetchone()
    return dict(row) if row else None


def _fetchall(sql, params=None):
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            return [dict(r) for r in cur.fetchall()]


# ──────────────────────────────────────────────────────────────────────────────
# SIGNAL DEDUP
# ──────────────────────────────────────────────────────────────────────────────
def is_new_signal(pair, sig):
    key = f"{pair}_{sig}"
    if key in last_signals:
        return False
    last_signals[key] = datetime.utcnow()
    return True


def prune_last_signals():
    cutoff = datetime.utcnow() - timedelta(hours=4)
    stale  = [k for k, ts in last_signals.items() if ts < cutoff]
    for k in stale:
        del last_signals[k]
    # Strategy 4: prune expired pending trades from the dedup dict too
    live_keys = {f"{t['pair']}_{t['signal']}" for t in pending_trades}
    extra = [k for k in list(last_signals) if k not in live_keys
             and last_signals[k] < datetime.utcnow() - timedelta(hours=1)]
    for k in extra:
        del last_signals[k]


def _restore_last_signals():
    """Repopulate last_signals from the last 4h of DB trades after restart."""
    cutoff = (datetime.utcnow() - timedelta(hours=4)).isoformat()
    try:
        rows = _fetchall(
            f"SELECT pair, signal, time FROM {TRADES_TABLE} WHERE time >= %(d)s",
            {"d": cutoff}
        )
        for row in rows:
            key = f"{row['pair']}_{row['signal']}"
            try:
                t = datetime.fromisoformat(str(row["time"]).replace(" ", "T"))
                last_signals[key] = t.replace(tzinfo=None)
            except Exception:
                pass
    except Exception:
        pass
    for t in pending_trades:
        last_signals[f"{t['pair']}_{t['signal']}"] = t.get("time", datetime.utcnow())


# ──────────────────────────────────────────────────────────────────────────────
# CAPACITY / LOSS GUARDS  (raw SQL — no pandas)
# ──────────────────────────────────────────────────────────────────────────────
def at_max_capacity():
    try:
        row = _fetchone(
            f"SELECT COUNT(*) AS cnt FROM {TRADES_TABLE} WHERE status = 'OPEN'"
        )
        open_count = int(row["cnt"]) if row else 0
    except Exception:
        open_count = 0
    return len(pending_trades) + open_count >= MAX_CONCURRENT


def daily_loss_limit_hit():
    return get_daily_losses() >= MAX_DAILY_LOSSES


def _in_loss_cooldown():
    """Block new entries for 15 min after any stop loss."""
    try:
        cutoff = (datetime.utcnow() - timedelta(minutes=15)).isoformat()
        row = _fetchone(
            f"SELECT COUNT(*) AS cnt FROM {TRADES_TABLE} "
            f"WHERE status = 'LOSS' AND time >= %(c)s",
            {"c": cutoff}
        )
        return int(row["cnt"]) > 0 if row else False
    except Exception:
        return False


def _directional_count(sig):
    try:
        row = _fetchone(
            f"SELECT COUNT(*) AS cnt FROM {TRADES_TABLE} "
            f"WHERE status = 'OPEN' AND signal = %(s)s",
            {"s": sig}
        )
        open_count = int(row["cnt"]) if row else 0
    except Exception:
        open_count = 0
    return open_count + sum(1 for t in pending_trades if t["signal"] == sig)


def _had_tp1_hit_today(symbol):
    try:
        today = datetime.utcnow().date().isoformat()
        row = _fetchone(
            f"SELECT tp1_hit FROM {TRADES_TABLE} "
            f"WHERE pair = %(pair)s AND status != 'OPEN' AND time >= %(today)s "
            f"ORDER BY time DESC LIMIT 1",
            {"pair": symbol, "today": today}
        )
        if row is None:
            return False
        val = row.get("tp1_hit")
        return bool(val) if val is not None else False
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────────
# PRICE FETCH via HTTP  (strategy 2: replace ccxt single-call with requests.get)
# ──────────────────────────────────────────────────────────────────────────────
def get_price(symbol, market_type):
    """Fetch last price via exchange REST API without loading ccxt."""
    try:
        if market_type == "spot":
            # KuCoin public ticker — no auth required
            sym_dash = symbol.replace("/", "-").split(":")[0]
            r = requests.get(
                "https://api.kucoin.com/api/v1/market/stats",
                params={"symbol": sym_dash},
                timeout=6,
            )
            data = r.json().get("data") or {}
            price = data.get("last") or data.get("lastTradedPrice")
        else:
            # MEXC futures — symbol format BTC_USDT
            sym_mx = symbol.replace("/USDT:USDT", "_USDT").replace("/", "_")
            r = requests.get(
                "https://contract.mexc.com/api/v1/contract/ticker",
                params={"symbol": sym_mx},
                timeout=6,
            )
            d = r.json()
            data  = d.get("data") or {}
            price = data.get("lastPrice") or data.get("last")
        return float(price) if price else None
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY HIT CHECK  (no candle fetch — uses current price from REST)
# ──────────────────────────────────────────────────────────────────────────────
def _check_entry_hit(price, entry, direction, trade_type):
    """Simplified entry check using current price only (no OHLCV needed)."""
    if price is None:
        return False
    if direction == "BUY":
        return price <= entry * 1.003   # 0.3% tolerance for bid/ask spread
    return price >= entry * 0.997


# ──────────────────────────────────────────────────────────────────────────────
# POSITION SIZING
# ──────────────────────────────────────────────────────────────────────────────
def calc_position_size(entry, sl, rr=0.0, conf=50):
    base_risk    = 0.01 + (conf / 100) * 0.02
    rr_bonus     = max(0.0, rr - 3.0) * 0.005
    risk_pct     = min(base_risk + rr_bonus, 0.05)
    risk_dollars = round(ACCOUNT_BALANCE * risk_pct, 4)
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return risk_dollars, 0.0, 0.0, risk_pct
    units         = risk_dollars / risk_per_unit
    position_value = round(units * entry, 4)
    return risk_dollars, round(units, 6), position_value, risk_pct


def _fmt_price(p):
    if p >= 1000:   return f"{p:,.2f}"
    if p >= 1:      return f"{p:.4f}"
    if p >= 0.01:   return f"{p:.6f}"
    if p >= 0.0001: return f"{p:.8f}"
    return f"{p:.10f}"


# ──────────────────────────────────────────────────────────────────────────────
# PENDING TRADE MANAGEMENT
# ──────────────────────────────────────────────────────────────────────────────
def check_pending_trades():
    global pending_trades
    updated = []
    for trade in pending_trades:
        symbol      = trade["pair"]
        market_type = trade["market_type"]

        expiry_hours = 3 if trade.get("trade_type") == "bounce" else 1
        if datetime.now() - trade["time"] > timedelta(hours=expiry_hours):
            print(f"Expired ({expiry_hours}h): {symbol}")
            continue

        price = get_price(symbol, market_type)

        # Trend breakout validation: if the breakout level (prev_high for BUY,
        # prev_low for SELL) has been violated by >0.3%, the breakout failed.
        # Entering now would be buying a falling knife — remove the signal.
        eva = trade.get("entry_valid_above")
        evb = trade.get("entry_valid_below")
        if price is not None:
            if eva and trade["signal"] == "BUY" and price < eva * 0.997:
                print(f"Breakout invalidated: {symbol} price {price:.6f} < breakout level {eva:.6f} — removed")
                continue
            if evb and trade["signal"] == "SELL" and price > evb * 1.003:
                print(f"Breakdown invalidated: {symbol} price {price:.6f} > breakdown level {evb:.6f} — removed")
                continue

        if not _check_entry_hit(price, trade["entry"], trade["signal"],
                                trade.get("trade_type", "trend")):
            updated.append(trade)
            continue

        # Late-entry guard: price must be within 1.5% of queued entry
        if price and abs(price - trade["entry"]) / trade["entry"] > 0.015:
            print(f"Late entry skipped: {symbol}")
            continue

        print(f"ENTRY HIT: {symbol}")
        direction    = "LONG" if trade["signal"] == "BUY" else "SHORT"
        market_label = "Spot" if market_type == "spot" else "Futures"
        tp2          = trade.get("tp2")
        tp2_line     = f"TP2     {_fmt_price(tp2)}\n" if tp2 else ""
        send_telegram(
            f"ENTRY TRIGGERED\n{'─'*22}\n"
            f"{symbol}  {direction}  [{market_label}]\n\n"
            f"Entry   {_fmt_price(trade['entry'])}\n"
            f"SL      {_fmt_price(trade['sl'])}\n"
            f"TP1     {_fmt_price(trade['tp'])}  <- close 50%\n"
            f"{tp2_line}"
            f"RR      1 : {trade['rr']}\n{'─'*22}\n"
            f"Trade is now live. Managing SL/TP."
        )
        _conf = trade.get("confidence", 50)
        _rd, _, _, _ = calc_position_size(
            trade["entry"], trade["sl"], trade.get("rr", 0.0), conf=_conf
        )
        save_trade(
            trade["pair"], trade["signal"], trade["entry"],
            trade["sl"], trade["tp"], tp2, trade["rr"],
            trade["market_type"], trade.get("atr", 0.0), _rd,
            confidence=_conf, plan=BOT_PLAN,
        )

    pending_trades = updated


# ──────────────────────────────────────────────────────────────────────────────
# MARKET MODE TRANSITIONS  (Telegram notifications only — computation is in worker)
# ──────────────────────────────────────────────────────────────────────────────
def _handle_mode_transition(new_mode, bear_breadth, bear_count, total_pairs):
    global _market_mode, _bear_mode_scans, _recovery_scans
    prev_mode = _market_mode
    _market_mode = new_mode
    if new_mode == prev_mode:
        return
    if new_mode == "bear":
        send_telegram(
            f"BEAR MODE ACTIVATED\n"
            f"bear_breadth: {bear_breadth:.0%} ({bear_count}/{total_pairs} below ema200)\n\n"
            f"Changes active:\n"
            f"• SELL signals on confirmed 4h downtrend pairs only\n"
            f"• Bounce BUY at structural support still enabled\n"
            f"• Fade-resistance SELL at 4h EMA50 enabled\n"
            f"• ADX gate -2/-3, vol threshold 0.80-0.90x, BB/coil skipped for SELL"
        )
    elif new_mode == "recovery":
        send_telegram(
            f"RECOVERY MODE ACTIVATED\n"
            f"bear_breadth: {bear_breadth:.0%} ({bear_count}/{total_pairs} below ema200)\n\n"
            f"Changes active:\n"
            f"• BUY signals only\n"
            f"• RR minimum 2.0 (all regimes)\n"
            f"• Bounce BUY: candle mandatory + AND gate + 0.5xATR SL buffer"
        )
    elif prev_mode in ("bear", "recovery"):
        send_telegram(
            f"NORMAL MODE RESTORED\n"
            f"bear_breadth: {bear_breadth:.0%}\n"
            f"All standard parameters in effect."
        )


def _handle_btc_transition(new_down, ema50, ema200):
    global _btc_downtrend, _btc_downtrend_prev
    if new_down == _btc_downtrend:
        return
    _btc_downtrend_prev = _btc_downtrend
    _btc_downtrend = new_down
    sep_pct = abs(ema50 - ema200) / ema200 * 100 if ema200 else 0
    if new_down:
        send_telegram(
            f"BTC MACRO: EMA50 CROSSED BELOW EMA200\n"
            f"EMA50={ema50:.1f}  EMA200={ema200:.1f}  (-{sep_pct:.2f}%)\n\n"
            f"BUY trend/reversal signals blocked.\n"
            f"Pullback & bounce BUY at structural support still enabled."
        )
    else:
        send_telegram(
            f"BTC MACRO: EMA50 CROSSED ABOVE EMA200\n"
            f"EMA50={ema50:.1f}  EMA200={ema200:.1f}  (+{sep_pct:.2f}%)\n\n"
            f"BUY trend/reversal signals re-enabled."
        )


# ──────────────────────────────────────────────────────────────────────────────
# SUBPROCESS SCANNER  (strategy 1: subprocess isolation)
# ──────────────────────────────────────────────────────────────────────────────
def _run_screener():
    """
    Spawn screener_worker.py as a subprocess. All heavy libraries load inside
    the child process; when it exits, the OS reclaims all that memory.

    Persistent state is passed via env vars so the worker can compute
    market_mode debounce counters correctly.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="scan_")
    os.close(fd)
    env = os.environ.copy()
    env["PREV_MARKET_MODE"]     = _market_mode
    env["PREV_BEAR_SCANS"]      = str(_bear_mode_scans)
    env["PREV_RECOVERY_SCANS"]  = str(_recovery_scans)
    env["PREV_BTC_DOWNTREND"]   = "true" if _btc_downtrend else "false"

    rss_before = _rss_kb()
    try:
        result = subprocess.run(
            [sys.executable, WORKER_SCRIPT, tmp_path],
            env=env,
            timeout=240,   # 4-minute wall-clock cap
        )
        rss_after = _rss_kb()
        print(f"[main] RSS: {rss_before:,} → {rss_after:,} kB "
              f"(delta {rss_after - rss_before:+,} kB after worker exit)")

        if result.returncode != 0:
            print(f"Screener worker exited with code {result.returncode}")
            return None

        with open(tmp_path) as f:
            return json.load(f)

    except subprocess.TimeoutExpired:
        print("Screener worker timed out (240s)")
        return None
    except Exception as e:
        print(f"Screener error: {e}")
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        gc.collect()   # strategy 3: collect in main process after worker exits


# ──────────────────────────────────────────────────────────────────────────────
# SIGNAL PROCESSING  (called after _run_screener returns JSON)
# ──────────────────────────────────────────────────────────────────────────────
def _process_scan_results(scan_data):
    global pending_trades, _market_mode, _btc_downtrend, _btc_downtrend_prev
    global _bear_mode_scans, _recovery_scans

    if not scan_data:
        return

    # Update persistent debounce counters from worker output
    _bear_mode_scans = scan_data.get("new_bear_scans", _bear_mode_scans)
    _recovery_scans  = scan_data.get("new_recovery_scans", _recovery_scans)

    _handle_mode_transition(
        scan_data.get("market_mode", "normal"),
        scan_data.get("bear_breadth", 0.0),
        scan_data.get("bear_count", 0),
        scan_data.get("total_pairs", 1),
    )

    btc_down  = scan_data.get("btc_downtrend")
    btc_e50   = scan_data.get("btc_ema50")
    btc_e200  = scan_data.get("btc_ema200")
    if btc_down is not None and btc_e50 and btc_e200:
        _handle_btc_transition(btc_down, btc_e50, btc_e200)

    hour_utc         = datetime.utcnow().hour
    session_filtered = 8 <= hour_utc <= 17

    for s in scan_data.get("signals", []):
        symbol      = s["pair"]
        market_type = s["market_type"]
        sig         = s["signal"]
        entry       = s["entry"]
        sl          = s["sl"]
        tp          = s["tp"]
        tp2         = s.get("tp2")
        rr          = s["rr"]
        atr         = s["atr"]
        trade_type  = s["trade_type"]
        conf        = s["confidence"]
        mkt_info    = s.get("market_info", {})

        # Re-apply quality gates (worker also applied them, but re-check for safety)
        if conf < 55:
            continue
        if session_filtered and conf < 65:
            continue

        if any(t["pair"] == symbol for t in pending_trades):
            print(f"Already pending: {symbol}")
            continue

        try:
            rows = _fetchall(
                f"SELECT pair FROM {TRADES_TABLE} "
                f"WHERE status = 'OPEN' AND pair = %(pair)s",
                {"pair": symbol}
            )
            if rows:
                print(f"Already active: {symbol}")
                continue
        except Exception:
            pass

        if not is_new_signal(symbol, sig):
            if not _had_tp1_hit_today(symbol):
                continue
            print(f"Re-entry allowed (TP1 hit today): {symbol}")

        if _directional_count(sig) >= 5:
            print(f"{symbol} skipped — {sig} direction full (5/5)")
            continue

        sl_pct   = abs(sl - entry) / entry * 100
        tp1_pct  = abs(tp - entry) / entry * 100
        direction = "🟢 LONG" if sig == "BUY" else "🔴 SHORT"
        exchange  = "KuCoin" if market_type == "spot" else "MEXC"
        mkt_label = f"{'Spot' if market_type=='spot' else 'Futures'} · {exchange}"
        now_str   = datetime.utcnow().strftime("%d %b %Y · %H:%M UTC")

        risk_dollars, units, pos_value, risk_pct = calc_position_size(entry, sl, rr, conf=conf)

        if pos_value > ACCOUNT_BALANCE * 10:
            print(f"{symbol} skipped — position ${pos_value:.2f} > 10× account")
            continue

        if market_type == "spot":
            min_qty  = mkt_info.get("min_qty", 0.001)
            quantity = round(units, 4)
            if quantity < min_qty:
                print(f"{symbol} skipped — {quantity} units below min ({min_qty})")
                continue
            size_line     = f"Size    {quantity} units  (~${pos_value:.2f})\n"
            leverage_line = ""
            funding_line  = ""
        else:
            cs        = mkt_info.get("contract_size", 1)
            min_qty   = mkt_info.get("min_qty", 1)
            contracts = round(units / cs, 1)
            if contracts < min_qty:
                print(f"{symbol} skipped — {contracts} contracts below min ({min_qty})")
                continue
            leverage      = max(1, round(pos_value / max(ACCOUNT_BALANCE, 1)))
            size_line     = f"Size    {contracts} contracts  (~${pos_value:.2f})\n"
            leverage_line = f"Leverage  set {leverage}x  on MEXC before entering\n"
            # Strategy 2: funding rate via direct HTTP instead of ccxt.fetch_funding_rate
            try:
                sym_mx = symbol.replace("/USDT:USDT", "_USDT").replace("/", "_")
                fr_resp = requests.get(
                    f"https://contract.mexc.com/api/v1/contract/funding_rate/{sym_mx}",
                    timeout=6
                )
                fr_data  = fr_resp.json().get("data") or {}
                fr_val   = float(fr_data.get("fundingRate", 0)) * 100
                funding_line = f"Funding   {fr_val:+.4f}%/8h  <- use isolated margin\n"
            except Exception:
                funding_line = "Funding   n/a  <- use isolated margin\n"

        tp2_line = (
            f"TP2     {_fmt_price(tp2)}  (^ {abs(tp2-entry)/entry*100:.2f}%)\n"
            if tp2 else ""
        )

        if trade_type == "bounce":
            msg = (
                f"{'─'*22}\n{direction}  [{trade_type.upper()}]  MARKET ENTRY\n{'─'*22}\n"
                f"Pair    {symbol}\nMarket  {mkt_label}\nTime    {now_str}\n\n"
                f"Entry   {_fmt_price(entry)}  <- enter NOW at market\n"
                f"SL      {_fmt_price(sl)}  (v {sl_pct:.2f}%)\n"
                f"TP1     {_fmt_price(tp)}  (^ {tp1_pct:.2f}%)  <- close 50%\n"
                f"{tp2_line}RR      1 : {rr}\nConf    {conf}/100\n{'─'*22}\n"
                f"POSITION SIZING  ({risk_pct*100:.0f}% risk)\n"
                f"Risk    ${risk_dollars:.2f}  of ${ACCOUNT_BALANCE:.2f}\n"
                f"{size_line}{leverage_line}{funding_line}{'─'*22}\n"
                f"Trade is now live. Managing SL/TP."
            )
            print(msg)
            send_telegram(msg)
            save_trade(symbol, sig, entry, sl, tp, tp2, rr, market_type,
                       float(atr), risk_dollars, confidence=conf, plan=BOT_PLAN)
        else:
            msg = (
                f"{'─'*22}\n{direction}  [{trade_type.upper()}]\n{'─'*22}\n"
                f"Pair    {symbol}\nMarket  {mkt_label}\nTime    {now_str}\n\n"
                f"Entry   {_fmt_price(entry)}\n"
                f"SL      {_fmt_price(sl)}  (v {sl_pct:.2f}%)\n"
                f"TP1     {_fmt_price(tp)}  (^ {tp1_pct:.2f}%)  <- close 50%\n"
                f"{tp2_line}RR      1 : {rr}\nConf    {conf}/100\n{'─'*22}\n"
                f"POSITION SIZING  ({risk_pct*100:.0f}% risk)\n"
                f"Risk    ${risk_dollars:.2f}  of ${ACCOUNT_BALANCE:.2f}\n"
                f"{size_line}{leverage_line}{funding_line}{'─'*22}\n"
                f"Pending -- waiting for entry to trigger"
            )
            print(msg)
            send_telegram(msg)
            pending_trades.append({
                "pair":              symbol,
                "signal":            sig,
                "entry":             entry,
                "sl":                sl,
                "tp":                tp,
                "tp2":               tp2,
                "rr":                rr,
                "market_type":       market_type,
                "trade_type":        trade_type,
                "atr":               float(atr),
                "time":              datetime.now(),
                "confidence":        conf,
                "entry_valid_above": s.get("entry_valid_above"),
                "entry_valid_below": s.get("entry_valid_below"),
            })

    check_pending_trades()


# ──────────────────────────────────────────────────────────────────────────────
# TELEGRAM COMMANDS
# ──────────────────────────────────────────────────────────────────────────────
last_update_id = 0


def check_telegram_commands():
    global pending_trades, last_update_id
    from logger import get_updates
    updates = get_updates(last_update_id + 1)
    for update in updates:
        last_update_id = update["update_id"]
        msg  = update.get("message", {})
        text_msg = msg.get("text", "").strip()
        chat = str(msg.get("chat", {}).get("id", ""))
        if chat != str(CHAT_ID):
            continue

        if text_msg == "/status":
            _handle_status()
        elif text_msg == "/stats":
            from performance import get_stats_summary
            send_telegram(get_stats_summary())
        elif text_msg == "/help":
            send_telegram(
                "Commands:\n"
                "/status -- open & pending trades\n"
                "/stats  -- win rate, expectancy, all-time edge\n"
                "/cancel SYMBOL -- remove pending signal\n"
                "/help -- this message"
            )
        elif text_msg.startswith("/cancel "):
            _handle_cancel(text_msg.split(" ", 1)[1].strip().upper())


def _handle_status():
    try:
        rows = _fetchall(
            f"SELECT pair, signal, entry, rr FROM {TRADES_TABLE} WHERE status = 'OPEN'"
        )
        open_list = (
            "\n".join(f"  {r['pair']} {r['signal']} @ {float(r['entry']):.6f} RR:{r['rr']}"
                      for r in rows) or "  None"
        )
        pend_list = (
            "\n".join(f"  {t['pair']} {t['signal']} @ {t['entry']:.6f}"
                      for t in pending_trades) or "  None"
        )
        send_telegram(f"STATUS\n\nOpen:\n{open_list}\n\nPending:\n{pend_list}")
    except Exception as e:
        send_telegram(f"Status error: {e}")


def _handle_cancel(symbol):
    global pending_trades
    before = len(pending_trades)
    pending_trades = [t for t in pending_trades if t["pair"] != symbol]
    if len(pending_trades) < before:
        save_pending_trades(pending_trades)
        send_telegram(f"Cancelled pending: {symbol}")
    else:
        send_telegram(f"No pending trade for: {symbol}")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────────────────────────────────────
def main():
    global pending_trades, ACCOUNT_BALANCE

    ensure_tables()
    pending_trades = load_pending_trades()
    _restore_last_signals()
    print(f"Loaded {len(pending_trades)} pending trade(s) from DB")
    print(f"[main] RSS at startup: {_rss_kb():,} kB  "
          f"(no ccxt/pandas/numpy imported in main process)")

    last_report_day = None

    while True:
        try:
            check_telegram_commands()

            ACCOUNT_BALANCE = get_compounded_balance(STARTING_BALANCE)
            if not ACCOUNT_BALANCE or ACCOUNT_BALANCE <= 0:
                ACCOUNT_BALANCE = STARTING_BALANCE
            print(f"Balance: ${ACCOUNT_BALANCE:.2f}  (started ${STARTING_BALANCE:.2f})")

            if daily_loss_limit_hit():
                print(f"Daily loss limit ({MAX_DAILY_LOSSES}) hit -- skipping new signals")
                check_pending_trades()
            else:
                hour_utc = datetime.utcnow().hour
                if 0 <= hour_utc <= 7:
                    print(f"Session gate (UTC {hour_utc:02d}xx -- blocked 00-07) -- trade management only")
                    check_pending_trades()
                else:
                    prune_last_signals()
                    if _in_loss_cooldown():
                        print("Loss cooldown active (15 min) -- no new entries this scan")
                        check_pending_trades()
                    elif at_max_capacity():
                        print(f"Max concurrent trades ({MAX_CONCURRENT}) reached, skipping scan")
                        check_pending_trades()
                    else:
                        print(f"\nScan: {datetime.now()}\n")
                        scan_data = _run_screener()
                        _process_scan_results(scan_data)
                        del scan_data
                        gc.collect()   # strategy 3: explicit GC after scan results processed

            check_trade_results(get_price, send_telegram)

            today = datetime.now().date()
            if last_report_day != today:
                # Lazy-import performance — loads pandas only here (once per day)
                from performance import daily_report
                daily_report(send_telegram)
                from logger import send_csv
                send_csv(TOKEN, CHAT_ID)
                last_report_day = today
                gc.collect()  # collect after pandas-heavy daily report

            save_pending_trades(pending_trades)

        except Exception as e:
            print(f"Loop error: {e}")
            send_telegram(f"Bot error (will retry in 60s):\n{e}")
            time.sleep(60)
            continue

        hour = datetime.utcnow().hour
        if hour in (18, 19, 20, 21, 22, 23):
            sleep_secs, session_tag = 300,  "premium"
        elif 8 <= hour <= 17:
            sleep_secs, session_tag = 600,  "filtered"
        else:
            sleep_secs, session_tag = 900,  "blocked"

        print(f"[main] RSS idle: {_rss_kb():,} kB -- sleeping {sleep_secs//60} min [{session_tag}]")
        time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
