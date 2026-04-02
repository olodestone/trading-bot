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
)
from logger import send_telegram, send_csv, TOKEN, CHAT_ID


# ==============================
# EXCHANGES
# ==============================
spot_exchange = ccxt.kucoin({"enableRateLimit": True, "rateLimit": 1200})
futures_exchange = ccxt.mexc({"enableRateLimit": True})

spot_exchange.options['adjustForTimeDifference'] = True
futures_exchange.options['adjustForTimeDifference'] = True

SPOT_MARKETS = spot_exchange.load_markets()
FUTURES_MARKETS = futures_exchange.load_markets()


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
last_signals = {}


def is_new_signal(pair, sig, entry):
    key = f"{pair}_{sig}_{round(entry, 6)}"
    if key in last_signals:
        return False
    last_signals[key] = time.time()
    return True


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
def momentum_score(symbol, market_type):
    """
    Score = ATR% * avg_volume (last 3 candles on 1h).
    High score = high volatility + high liquidity = best trading candidates.
    Minimum volume threshold filters illiquid pairs.
    """
    df = get_cached_tf(symbol, "1h", market_type)
    if df is None or len(df) < 20:
        return 0

    close = df['close'].iloc[-1]
    if close <= 0:
        return 0

    vol_avg = df['volume'].tail(3).mean()
    if vol_avg < 8000:  # minimum liquidity gate
        return 0

    hl = df['high'] - df['low']
    hc = (df['high'] - df['close'].shift()).abs()
    lc = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]

    atr_pct = (atr / close) * 100
    return atr_pct * vol_avg


def get_pairs():
    """
    Score all USDT pairs by momentum, return top 20.
    Uses first 80 symbols from each exchange to stay within rate limits.
    """
    candidates = []

    spot_syms = [s for s in list(SPOT_MARKETS)[:80] if "/USDT" in s and ":" not in s]
    futures_syms = [s for s in list(FUTURES_MARKETS)[:80] if "/USDT:USDT" in s]

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
            send_telegram(
                f"✅ ENTRY HIT\n{symbol}\nEntry: {trade['entry']:.6f}\nRR: {trade['rr']}"
            )
            save_trade(
                trade['pair'], trade['signal'], trade['entry'],
                trade['sl'], trade['tp'], trade['rr'],
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

    print(f"\n🚀 Scan: {datetime.now()}\n")

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

        df_15m = apply_indicators(df_15m)
        df_1h = apply_indicators(df_1h)
        df_4h = apply_indicators(df_4h)
        df_1d = apply_indicators(df_1d)

        result = generate_filtered_signal(df_15m, df_1h, df_4h, df_1d)
        if not result:
            continue

        sig, entry, sl, tp, rr, atr, trade_type = result

        # Skip if already pending
        if any(t['pair'] == symbol for t in pending_trades):
            print(f"⚠️ Already pending: {symbol}")
            continue

        # Skip if already active in DB
        try:
            from performance import get_engine, TRADES_TABLE
            import sqlalchemy
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

        if not is_new_signal(symbol, sig, entry):
            continue

        msg = f"""
🎯 SIGNAL
Pair:   {symbol}
Signal: {sig}
Entry:  {entry:.6f}
SL:     {sl:.6f}
TP:     {tp:.6f}
RR:     {rr}
Type:   {trade_type}
ATR:    {atr:.6f}
"""
        print(msg)
        send_telegram(msg)

        pending_trades.append({
            "pair": symbol,
            "signal": sig,
            "entry": entry,
            "sl": sl,
            "tp": tp,
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
