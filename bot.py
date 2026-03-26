import ccxt
import pandas as pd
import time
import requests
from datetime import datetime, timedelta
import os
import signal

from strategy import apply_indicators, generate_filtered_signal
from performance import save_trade, check_trade_results, daily_report, ensure_csv
from performance import send_csv

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ==============================
# TELEGRAM
# ==============================
def send_telegram(msg):
    if not TOKEN or not CHAT_ID:
        print("⚠️ Telegram not configured")
        return
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg})
    except Exception as e:
        print("Telegram error:", e)

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

def get_cached_tf(symbol, tf, market_type):
    key = f"{symbol}_{tf}_{market_type}"
    now = time.time()

    if tf == "1h":
        refresh_time = 900
    elif tf == "4h":
        refresh_time = 14400
    elif tf == "1d":
        refresh_time = 86400
    else:
        refresh_time = 0

    if key in HTF_CACHE and (now - HTF_LAST_UPDATE[key] < refresh_time):
        return HTF_CACHE[key]

    df, _ = fetch_tf(symbol, tf, market_type)

    if df is not None:
        HTF_CACHE[key] = df
        HTF_LAST_UPDATE[key] = now

    return df

# ==============================
# PENDING TRADES
# ==============================
pending_trades = []

# ==============================
# DUPLICATE FILTER
# ==============================
last_signals = {}

def is_new_signal(pair, signal, entry):
    key = f"{pair}_{signal}_{round(entry,6)}"
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
    ex = spot_exchange if market_type == "spot" else futures_exchange

    for i in range(2):
        try:
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(10)

            data = ex.fetch_ohlcv(symbol, tf, limit=100)
            signal.alarm(0)

            df = pd.DataFrame(data, columns=['time','open','high','low','close','volume'])
            return df, ex.id

        except Exception as e:
            signal.alarm(0)
            if "429" in str(e):
                print("⚠️ Rate limit hit, cooling down...")
                time.sleep(10)

            print(f"Fetch retry {i+1} {symbol} {tf}: {e}")
            time.sleep(2)

    return None, None

# ==============================
# ENTRY CHECK (HIGH/LOW FIX)
# ==============================
def entry_hit(df, entry, direction, trade_type):
    if df is None or df.empty or len(df) < 2:
        return False

    last = df.iloc[-1]
    prev = df.iloc[-2]

    if trade_type == "trend":
        if direction == "BUY":
            return last['low'] <= entry
        elif direction == "SELL":
            return last['high'] >= entry

    elif trade_type == "reversal":
        if direction == "BUY":
            return (
                last['low'] <= entry
                and last['close'] > prev['high']
                and last['close'] > last['open']
            )
        elif direction == "SELL":
            return (
                last['high'] >= entry
                and last['close'] < prev['low']
                and last['close'] < last['open']
            )

    return False


def is_not_late_entry(df, entry, direction):
    if df is None or df.empty:
        return False

    last = df.iloc[-1]
    price = last['close']

    max_distance = 0.004  # 0.4% (tweakable)

    distance = abs(price - entry) / entry

    if distance > max_distance:
        return False

    return True    

# ==============================
# GET PAIRS
# ==============================
def get_pairs():
    pairs = []

    for symbol in list(SPOT_MARKETS)[:40]:
        if "/USDT" in symbol and ":" not in symbol:
            df = get_cached_tf(symbol, "1h", "spot")
            if df is not None and df['volume'].tail(3).mean() > 5000:
                pairs.append((symbol, "spot"))

    for symbol in list(FUTURES_MARKETS)[:40]:
        if "/USDT:USDT" in symbol:
            df = get_cached_tf(symbol, "1h", "futures")
            if df is not None and df['volume'].tail(3).mean() > 5000:
                pairs.append((symbol, "futures"))

    return pairs[:8]

# ==============================
# CHECK PENDING TRADES
# ==============================
def check_pending_trades():
    global pending_trades

    updated = []

    for trade in pending_trades:
        symbol = trade['pair']
        market_type = trade['market_type']

        df, _ = fetch_tf(symbol, "15m", market_type)

        # 🔥 Safety check
        if df is None or df.empty:
            updated.append(trade)
            continue

        # 🔥 Expiry (24h)
        if datetime.now() - trade['time'] > timedelta(hours=24):
            print(f"❌ Expired: {symbol}")
            continue

        if entry_hit(df, trade['entry'], trade['signal'], trade['trade_type']):

            # 🔥 Block late entries FIRST
            if not is_not_late_entry(df, trade['entry'], trade['signal']):
                print(f"⚠️ Skipped late entry: {symbol} (price moved too far)")
                continue

            # ✅ Valid entry
            print(f"✅ ENTRY HIT (DELAYED): {symbol}")
            send_telegram(
                f"✅ ENTRY HIT\n{symbol}\nEntry: {trade['entry']}\nRR: {trade['rr']}"
            )

            save_trade(
                trade['pair'],
                trade['signal'],
                trade['entry'],
                trade['sl'],
                trade['tp'],
                trade['rr'],
                trade['market_type']
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
    signals = []

    for symbol, market_type in pairs:
        df_15m, source = fetch_tf(symbol, "15m", market_type)
        df_1h = get_cached_tf(symbol, "1h", market_type)
        df_4h = get_cached_tf(symbol, "4h", market_type)
        df_1d = get_cached_tf(symbol, "1d", market_type)

        if any(x is None or x.empty for x in [df_15m, df_1h, df_4h, df_1d]):
            continue

        df_15m = apply_indicators(df_15m)
        df_1h = apply_indicators(df_1h)
        df_4h = apply_indicators(df_4h)
        df_1d = apply_indicators(df_1d)

        result = generate_filtered_signal(df_15m, df_1h, df_4h, df_1d)
        if not result:
            continue

        signal, entry, sl, tp, rr, trade_type = result

        if not is_new_signal(symbol, signal, entry):
            continue

        msg = f"""
🚀 ELITE SIGNAL
Pair: {symbol}
Signal: {signal}
Entry: {entry}
SL: {sl}
TP: {tp}
RR: {rr}
Trade Type: {trade_type}
"""

        send_telegram(msg)

        pending_trades.append({
            "pair": symbol,
            "signal": signal,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "market_type": market_type,
            "trade_type": trade_type,
            "time": datetime.now()
        })

    # 🔥 CHECK PENDING EVERY SCAN
    check_pending_trades()

# ==============================
# GET PRICE (FIX - CORRECT POSITION)
# ==============================
def get_price(symbol, market_type):
    df, _ = fetch_tf(symbol, "15m", market_type)

    if df is None or df.empty:
        return None

    return df.iloc[-1]['close']  

# ==============================
# LOOP
# ==============================
def main():
    ensure_csv()
    last_report_day = None

    while True:
        run_bot()

        check_trade_results(get_price, send_telegram)

        today = datetime.now().date()
        if last_report_day != today:
            daily_report(send_telegram)
            send_csv(TOKEN, CHAT_ID)
            last_report_day = today

        print("⏳ Sleeping for 15 minutes...")
        time.sleep(900)

# ==============================
# START
# ==============================
if __name__ == "__main__":
    main()