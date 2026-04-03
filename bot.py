import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta
import os
import signal as signal_module

from strategy import apply_indicators, generate_filtered_signal
from performance import (
    save_trade, check_trade_results, daily_report,
    ensure_csv, save_pending_trades, load_pending_trades,
    get_daily_losses, get_engine, TRADES_TABLE, get_stats_summary,
)
from logger import send_telegram, send_csv, get_updates, TOKEN, CHAT_ID


# ==============================
# EXCHANGES
# ==============================
spot_exchange = ccxt.kucoin({"enableRateLimit": True, "rateLimit": 1200})
futures_exchange = ccxt.mexc({"enableRateLimit": True})

spot_exchange.options['adjustForTimeDifference'] = True
futures_exchange.options['adjustForTimeDifference'] = True

SPOT_MARKETS = spot_exchange.load_markets()
FUTURES_MARKETS = futures_exchange.load_markets()

MARKET_REFRESH_INTERVAL = 86400  # 24 hours
last_market_refresh = time.time()


# ==============================
# MARKET REFRESH
# ==============================
def refresh_markets_if_needed():
    global SPOT_MARKETS, FUTURES_MARKETS, last_market_refresh
    if time.time() - last_market_refresh > MARKET_REFRESH_INTERVAL:
        try:
            SPOT_MARKETS = spot_exchange.load_markets()
            FUTURES_MARKETS = futures_exchange.load_markets()
            last_market_refresh = time.time()
            print("🔄 Markets refreshed")
        except Exception as e:
            print(f"Market refresh error: {e}")


# ==============================
# CACHE
# ==============================
HTF_CACHE = {}
HTF_LAST_UPDATE = {}
MARKET_DATA = {}
PRICE_CACHE = {}

HTF_REFRESH = {"1h": 900, "4h": 14400, "1d": 86400}


def get_cached_tf(symbol, tf, market_type):
    key = f"{symbol}_{tf}_{market_type}"
    now = time.time()
    refresh = HTF_REFRESH.get(tf, 0)
    if key in HTF_CACHE and (now - HTF_LAST_UPDATE.get(key, 0) < refresh):
        return HTF_CACHE[key]
    df, _ = fetch_tf(symbol, tf, market_type)
    if df is not None:
        HTF_CACHE[key] = df
        HTF_LAST_UPDATE[key] = now
    return df


# ==============================
# PENDING TRADES + DEDUP
# ==============================
pending_trades = []
last_signals = {}  # key: "pair_signal_date" → True


def is_new_signal(pair, sig):
    today = str(datetime.now().date())
    key = f"{pair}_{sig}_{today}"
    if key in last_signals:
        return False
    last_signals[key] = True
    return True


def prune_last_signals():
    """Remove entries from previous days."""
    today = str(datetime.now().date())
    stale = [k for k in last_signals if not k.endswith(today)]
    for k in stale:
        del last_signals[k]


# ==============================
# CAPACITY + DAILY LOSS GUARDS
# ==============================
MAX_CONCURRENT = 5
MAX_DAILY_LOSSES = 3

# Mid-cap price filter — focus on explosive movers, exclude BTC/ETH and sub-cent noise
MID_CAP_MIN = 0.10   # $0.10 minimum price
MID_CAP_MAX = 150.0  # $150 maximum price

# ==============================
# POSITION SIZING CONFIG
# Set ACCOUNT_BALANCE and RISK_PCT as env vars, or edit defaults here.
# RISK_PCT = 0.02 means risk 2% of account per trade.
# ==============================
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "15"))
RISK_PCT = float(os.getenv("RISK_PCT", "0.02"))


def at_max_capacity():
    open_count = 0
    try:
        engine = get_engine()
        df = pd.read_sql(
            f"SELECT COUNT(*) as cnt FROM {TRADES_TABLE} WHERE status = 'OPEN'", engine
        )
        open_count = int(df['cnt'].iloc[0])
    except Exception:
        pass
    return len(pending_trades) + open_count >= MAX_CONCURRENT


def daily_loss_limit_hit():
    return get_daily_losses() >= MAX_DAILY_LOSSES


# ==============================
# TELEGRAM COMMAND HANDLING
# ==============================
last_update_id = 0


def check_telegram_commands():
    global pending_trades, last_update_id
    updates = get_updates(last_update_id + 1)
    for update in updates:
        last_update_id = update["update_id"]
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat = str(msg.get("chat", {}).get("id", ""))

        if chat != str(CHAT_ID):
            continue  # ignore messages from other chats

        if text == "/status":
            _handle_status()
        elif text == "/stats":
            send_telegram(get_stats_summary())
        elif text == "/help":
            send_telegram(
                "📖 Commands:\n"
                "/status — open & pending trades\n"
                "/stats  — win rate, expectancy, all-time edge\n"
                "/cancel SYMBOL — remove pending signal\n"
                "/help — this message"
            )
        elif text.startswith("/cancel "):
            symbol = text.split(" ", 1)[1].strip().upper()
            _handle_cancel(symbol)


def _handle_status():
    try:
        engine = get_engine()
        df = pd.read_sql(
            f"SELECT pair, signal, entry, rr FROM {TRADES_TABLE} WHERE status = 'OPEN'", engine
        )
        open_list = (
            "\n".join(f"  {r['pair']} {r['signal']} @ {r['entry']:.6f} RR:{r['rr']}"
                      for _, r in df.iterrows())
            or "  None"
        )
        pend_list = (
            "\n".join(f"  {t['pair']} {t['signal']} @ {t['entry']:.6f}"
                      for t in pending_trades)
            or "  None"
        )
        send_telegram(f"📊 STATUS\n\nOpen:\n{open_list}\n\nPending:\n{pend_list}")
    except Exception as e:
        send_telegram(f"Status error: {e}")


def _handle_cancel(symbol):
    global pending_trades
    before = len(pending_trades)
    pending_trades = [t for t in pending_trades if t['pair'] != symbol]
    if len(pending_trades) < before:
        save_pending_trades(pending_trades)
        send_telegram(f"✅ Cancelled pending: {symbol}")
    else:
        send_telegram(f"⚠️ No pending trade for: {symbol}")


# ==============================
# POSITION SIZING
# ==============================
def calc_position_size(entry, sl):
    """
    Returns (risk_dollars, units, position_value) based on ACCOUNT_BALANCE / RISK_PCT.
    risk_dollars = how much of the account is at risk on this trade.
    units        = how many coins/contracts to buy.
    position_value = notional value of the position.
    """
    risk_dollars = round(ACCOUNT_BALANCE * RISK_PCT, 4)
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return risk_dollars, 0.0, 0.0
    units = risk_dollars / risk_per_unit
    position_value = round(units * entry, 4)
    return risk_dollars, round(units, 6), position_value


# ==============================
# PRICE FORMATTER
# ==============================
def _fmt_price(p):
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


# ==============================
# FETCH
# ==============================
def timeout_handler(signum, frame):
    raise Exception("Timeout")


def fetch_tf(symbol, tf, market_type):
    key = f"{symbol}_{tf}_{market_type}"
    if key in MARKET_DATA:
        return MARKET_DATA[key]

    ex = spot_exchange if market_type == "spot" else futures_exchange

    for attempt in range(2):
        try:
            signal_module.signal(signal_module.SIGALRM, timeout_handler)
            signal_module.alarm(10)
            # fetch 200 candles so indicators (EMA200, ADX) have enough history
            data = ex.fetch_ohlcv(symbol, tf, limit=200)
            signal_module.alarm(0)

            df = pd.DataFrame(data, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            MARKET_DATA[key] = (df, ex.id)
            return df, ex.id

        except Exception as e:
            signal_module.alarm(0)
            if "429" in str(e):
                print("⚠️ Rate limit, cooling...")
                time.sleep(15)
            else:
                print(f"Fetch retry {attempt + 1} {symbol} {tf}: {e}")
                time.sleep(2)

    return None, None


# ==============================
# PRICE LOOKUP
# ==============================
def get_price(symbol, market_type):
    key = f"{symbol}_15m_{market_type}"
    if key not in MARKET_DATA:
        return None
    df, _ = MARKET_DATA[key]
    if df is None or df.empty:
        return None
    return df.iloc[-1]['close']


# ==============================
# MOMENTUM PAIR SELECTION
# ==============================
# STABLECOIN BLOCKLIST
# ==============================
_STABLES = {
    "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "FRAX", "USDP",
    "UST", "USDD", "SUSD", "GUSD", "LUSD", "PYUSD", "USDJ",
    "CUSD", "CEUR", "EURS", "ALUSD", "USDN", "MUSD", "USDX",
}

# Non-crypto commodity / metals perpetuals that appear on MEXC futures
_NON_CRYPTO = {
    "XAUT", "PAXG", "CACHE", "XAU", "XAG", "XAGT",   # gold / silver tokens
    "USOIL", "UKOIL", "OIL", "BRENT", "WTI",           # oil perpetuals
    "WHEAT", "CORN", "SOYB",                            # agricultural
}


def _is_stable(symbol):
    base = symbol.split("/")[0]
    return base in _STABLES


def _is_non_crypto(symbol):
    base = symbol.split("/")[0]
    return base in _NON_CRYPTO


# ==============================
# MOMENTUM PAIR SELECTION
# ==============================
def momentum_score(symbol, market_type):
    """
    Score = ATR% * recent 1h volume, boosted when volume is surging NOW.
    - vol_avg_usdt: last 3 candles (3h) — captures current activity
    - surge_mult: if last 1h vol > 20-candle avg, coin is heating up NOW
    """
    df = get_cached_tf(symbol, "1h", market_type)
    if df is None or len(df) < 20:
        return 0

    close = df['close'].iloc[-1]
    if close <= 0:
        return 0

    vol_avg_usdt = df['volume'].tail(3).mean() * close
    if vol_avg_usdt < 75_000:  # $75K USDT per candle minimum
        return 0

    # Volume surge: reward coins breaking out in volume RIGHT NOW
    vol_ma20 = df['volume'].tail(20).mean()
    last_vol = df['volume'].iloc[-1]
    surge_mult = min(last_vol / vol_ma20, 3.0) if vol_ma20 > 0 else 1.0

    hl = df['high'] - df['low']
    hc = (df['high'] - df['close'].shift()).abs()
    lc = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]

    atr_pct = (atr / close) * 100
    return atr_pct * vol_avg_usdt * surge_mult


def _get_liquid_active_pool(exchange, market_type, symbol_filter, top_n=50):
    """
    One fetch_tickers() call → filter stablecoins → gate on liquidity
    and recent movement → sort by 24h volume → return top N.

    Gates (pre-filter before momentum scoring):
      - 24h quoteVolume > $2M   : real liquidity floor
      - |24h % change| > 1.5%  : coin is MOVING today, not dead
    """
    try:
        tickers = exchange.fetch_tickers()
        pool = []
        for sym, t in tickers.items():
            if not symbol_filter(sym):
                continue
            if _is_stable(sym) or _is_non_crypto(sym):
                continue
            vol_24h = t.get("quoteVolume") or 0
            if vol_24h < 2_000_000:
                continue
            # percentage can be None on MEXC futures — skip movement gate
            # if data is unavailable rather than rejecting the whole pool
            pct_raw = t.get("percentage")
            if pct_raw is not None and abs(pct_raw) < 1.5:
                continue
            pool.append((sym, vol_24h))
        pool.sort(key=lambda x: x[1], reverse=True)
        result = [s for s, _ in pool[:top_n]]
        if not result:
            raise ValueError("empty pool after filters")
        return result
    except Exception as e:
        print(f"⚠️ fetch_tickers failed ({market_type}): {e} — using fallback")
        if market_type == "spot":
            return [s for s in SPOT_MARKETS
                    if symbol_filter(s) and not _is_stable(s) and not _is_non_crypto(s)][:top_n]
        return [s for s in FUTURES_MARKETS
                if symbol_filter(s) and not _is_stable(s) and not _is_non_crypto(s)][:top_n]


def get_pairs():
    """
    Pipeline:
      1. fetch_tickers() → liquid ($2M+) + active (1.5%+ move) USDT pairs
      2. Score top 50 by ATR% × 1h_vol × surge_multiplier
      3. Return best 20 — these go into the strategy
    """
    candidates = []

    spot_syms = _get_liquid_active_pool(
        spot_exchange, "spot",
        lambda s: "/USDT" in s and ":" not in s,
    )
    futures_syms = _get_liquid_active_pool(
        futures_exchange, "futures",
        lambda s: "/USDT:USDT" in s,
    )

    for symbol in spot_syms:
        score = momentum_score(symbol, "spot")
        if score > 0:
            candidates.append((symbol, "spot", score))

    for symbol in futures_syms:
        score = momentum_score(symbol, "futures")
        if score > 0:
            candidates.append((symbol, "futures", score))

    candidates.sort(key=lambda x: x[2], reverse=True)
    top = [(sym, mtype) for sym, mtype, _ in candidates[:20]]
    print(f"📊 Top pairs: {[s for s, _ in top]}")
    return top


# ==============================
# ENTRY HIT CHECK
# ==============================
def entry_hit(df, entry, direction, trade_type):
    if df is None or df.empty or len(df) < 2:
        return False
    last = df.iloc[-1]
    prev = df.iloc[-2]

    if trade_type == "trend":
        if direction == "BUY":
            return last['low'] <= entry
        return last['high'] >= entry

    if trade_type == "reversal":
        if direction == "BUY":
            return (
                last['low'] <= entry
                and last['close'] > prev['high']
                and last['close'] > last['open']
            )
        return (
            last['high'] >= entry
            and last['close'] < prev['low']
            and last['close'] < last['open']
        )

    return False


def is_not_late_entry(df, entry, direction):
    if df is None or df.empty:
        return False
    price = df.iloc[-1]['close']
    return abs(price - entry) / entry <= 0.005


# ==============================
# CHECK PENDING TRADES
# ==============================
def check_pending_trades():
    global pending_trades
    updated = []

    for trade in pending_trades:
        symbol = trade['pair']
        market_type = trade['market_type']

        if datetime.now() - trade['time'] > timedelta(hours=24):
            print(f"❌ Expired: {symbol}")
            continue

        key = f"{symbol}_15m_{market_type}"
        if key not in MARKET_DATA:
            updated.append(trade)
            continue

        df, _ = MARKET_DATA[key]
        if df is None or df.empty:
            updated.append(trade)
            continue

        if entry_hit(df, trade['entry'], trade['signal'], trade['trade_type']):
            if not is_not_late_entry(df, trade['entry'], trade['signal']):
                print(f"⚠️ Late entry skipped: {symbol}")
                continue

            print(f"✅ ENTRY HIT: {symbol}")
            direction = "LONG" if trade['signal'] == "BUY" else "SHORT"
            market_label = "Spot" if trade['market_type'] == "spot" else "Futures"
            tp2 = trade.get('tp2')
            tp2_line = f"TP2     {_fmt_price(tp2)}\n" if tp2 else ""
            send_telegram(
                f"✅ ENTRY TRIGGERED\n"
                f"{'─' * 22}\n"
                f"{symbol}  {direction}  [{market_label}]\n\n"
                f"Entry   {_fmt_price(trade['entry'])}\n"
                f"SL      {_fmt_price(trade['sl'])}\n"
                f"TP1     {_fmt_price(trade['tp'])}  ← close 50%\n"
                f"{tp2_line}"
                f"RR      1 : {trade['rr']}\n"
                f"{'─' * 22}\n"
                f"🔔 Trade is now live. Managing SL/TP."
            )
            save_trade(
                trade['pair'], trade['signal'], trade['entry'],
                trade['sl'], trade['tp'], tp2, trade['rr'],
                trade['market_type'], trade.get('atr', 0.0)
            )
        else:
            updated.append(trade)

    pending_trades = updated


# ==============================
# MAIN SCAN
# ==============================
def run_bot():
    global pending_trades

    prune_last_signals()

    if at_max_capacity():
        print(f"⚠️ Max concurrent trades ({MAX_CONCURRENT}) reached, skipping scan")
        check_pending_trades()
        return

    print(f"\n🚀 Scan: {datetime.now()}\n")

    refresh_markets_if_needed()
    pairs = get_pairs()

    for symbol, market_type in pairs:
        df_15m, source = fetch_tf(symbol, "15m", market_type)
        df_1h = get_cached_tf(symbol, "1h", market_type)
        df_4h = get_cached_tf(symbol, "4h", market_type)
        df_1d = get_cached_tf(symbol, "1d", market_type)

        if any(x is None or x.empty for x in [df_15m, df_1h, df_4h, df_1d]):
            continue
        # Need enough history for EMA200 + ADX warmup
        if any(len(x) < 50 for x in [df_15m, df_1h, df_4h, df_1d]):
            continue

        # Mid-cap filter: skip BTC/ETH (too slow) and sub-cent noise
        price_now = df_15m.iloc[-1]['close']
        if not (MID_CAP_MIN <= price_now <= MID_CAP_MAX):
            continue

        df_15m = apply_indicators(df_15m)
        df_1h = apply_indicators(df_1h)
        df_4h = apply_indicators(df_4h)
        df_1d = apply_indicators(df_1d)

        result = generate_filtered_signal(df_15m, df_1h, df_4h, df_1d)
        if not result:
            continue

        sig, entry, sl, tp, tp2, rr, atr, trade_type = result

        # Skip if already pending
        if any(t['pair'] == symbol for t in pending_trades):
            print(f"⚠️ Already pending: {symbol}")
            continue

        # Skip if already active in DB
        try:
            engine = get_engine()
            active = pd.read_sql(
                f"SELECT pair FROM {TRADES_TABLE} WHERE status = 'OPEN' AND pair = %(pair)s",
                engine, params={"pair": symbol}
            )
            if not active.empty:
                print(f"⚠️ Already active: {symbol}")
                continue
        except Exception:
            pass

        if not is_new_signal(symbol, sig):
            continue

        sl_pct = abs(sl - entry) / entry * 100
        tp1_pct = abs(tp - entry) / entry * 100
        direction = "🟢 LONG" if sig == "BUY" else "🔴 SHORT"
        exchange = "KuCoin" if market_type == "spot" else "MEXC"
        market_label = f"{'Spot' if market_type == 'spot' else 'Futures'} · {exchange}"
        now_str = datetime.utcnow().strftime("%d %b %Y · %H:%M UTC")

        risk_dollars, units, pos_value = calc_position_size(entry, sl)

        tp2_line = (
            f"TP2     {_fmt_price(tp2)}  (▲ {abs(tp2 - entry) / entry * 100:.2f}%)\n"
            if tp2 else ""
        )

        msg = (
            f"{'─' * 22}\n"
            f"{direction}  [{trade_type.upper()}]\n"
            f"{'─' * 22}\n"
            f"Pair    {symbol}\n"
            f"Market  {market_label}\n"
            f"Time    {now_str}\n\n"
            f"Entry   {_fmt_price(entry)}\n"
            f"SL      {_fmt_price(sl)}  (▼ {sl_pct:.2f}%)\n"
            f"TP1     {_fmt_price(tp)}  (▲ {tp1_pct:.2f}%)  ← close 50%\n"
            f"{tp2_line}"
            f"RR      1 : {rr}\n"
            f"{'─' * 22}\n"
            f"💰 POSITION SIZING  ({RISK_PCT*100:.0f}% risk)\n"
            f"Risk    ${risk_dollars:.2f}  of ${ACCOUNT_BALANCE:.2f}\n"
            f"Size    {units} units  (~${pos_value:.2f})\n"
            f"{'─' * 22}\n"
            f"⏳ Pending — waiting for entry to trigger"
        )
        print(msg)
        send_telegram(msg)

        pending_trades.append({
            "pair": symbol,
            "signal": sig,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "tp2": tp2,
            "rr": rr,
            "market_type": market_type,
            "trade_type": trade_type,
            "atr": float(atr),
            "time": datetime.now()
        })

    check_pending_trades()


# ==============================
# MAIN LOOP
# ==============================
def main():
    global pending_trades, PRICE_CACHE, MARKET_DATA

    ensure_csv()
    pending_trades = load_pending_trades()
    print(f"📂 Loaded {len(pending_trades)} pending trade(s) from DB")

    last_report_day = None

    while True:
        try:
            MARKET_DATA = {}
            PRICE_CACHE = {}

            check_telegram_commands()

            if daily_loss_limit_hit():
                print(f"🛑 Daily loss limit ({MAX_DAILY_LOSSES}) hit — skipping new signals")
            else:
                run_bot()

            check_trade_results(get_price, send_telegram)

            today = datetime.now().date()
            if last_report_day != today:
                daily_report(send_telegram)
                send_csv(TOKEN, CHAT_ID)
                last_report_day = today

            save_pending_trades(pending_trades)

        except Exception as e:
            print(f"⚠️ Loop error: {e}")
            send_telegram(f"⚠️ Bot error (will retry in 60s):\n{e}")
            time.sleep(60)
            continue

        print("⏳ Sleeping 15 minutes...")
        time.sleep(900)


if __name__ == "__main__":
    main()
