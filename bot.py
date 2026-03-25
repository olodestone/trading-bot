import ccxt
import pandas as pd
import time
import requests
from datetime import datetime
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
spot_exchange = ccxt.kucoin({
    "enableRateLimit": True,
    "rateLimit": 1200,
})
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

    # 🔥 Optimized refresh timing
    if tf == "1h":
        refresh_time = 900      # 15 mins (NOT 1h)
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
# FETCH DATA WITH TIMEOUT
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

    print(f"❌ Final fetch fail {symbol} {tf}")
    return None, None

# ==============================
# GET PRICE
# ==============================
def get_price(symbol, market_type):
    df, _ = fetch_tf(symbol, "15m", market_type)
    if df is None or df.empty:
        return None
    return df.iloc[-1]['close']

# ==============================
# ENTRY CHECK (FIXED - NO EXTRA API CALL)
# ==============================
def entry_hit(df, entry, direction, trade_type):

    if df is None or df.empty or len(df) < 2:
        return False

    last = df.iloc[-1]
    prev = df.iloc[-2]

    tolerance = 0.003
    price = last['close']

    if trade_type == "trend":
        if direction == "BUY":
            return price <= entry * (1 + tolerance)
        elif direction == "SELL":
            return price >= entry * (1 - tolerance)

    elif trade_type == "reversal":
        if direction == "BUY":
            return (
                price <= entry * (1 + tolerance)
                and last['close'] > prev['high']
                and last['close'] > last['open']
            )
        elif direction == "SELL":
            return (
                price >= entry * (1 - tolerance)
                and last['close'] < prev['low']
                and last['close'] < last['open']
            )

    return False

# ==============================
# GET PAIRS
# ==============================
def get_pairs():
    pairs = []

    try:
        for symbol in SPOT_MARKETS:
            if "/USDT" in symbol and ":" not in symbol:

                df = get_cached_tf(symbol, "1h", "spot")
                time.sleep(0.8)

                if df is None or df.empty or len(df) < 3:
                    continue

                if df['volume'].tail(3).mean() > 5000:
                    pairs.append((symbol, "spot"))

    except Exception as e:
        print("Spot error:", e)

    try:
        for symbol in FUTURES_MARKETS:
            if "/USDT:USDT" in symbol:

                df = get_cached_tf(symbol, "1h", "futures")
                time.sleep(0.8)

                if df is None or df.empty or len(df) < 3:
                    continue

                if df['volume'].tail(3).mean() > 5000:
                    pairs.append((symbol, "futures"))

    except Exception as e:
        print("Futures error:", e)

    return pairs[:8]

# ==============================
# MAIN SCAN
# ==============================
def run_bot():

    print(f"\n🚀 Scan: {datetime.now()}\n")

    pairs = get_pairs()
    signals = []

    for symbol, market_type in pairs:
        time.sleep(1.5)

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

        signals.append({
            "pair": symbol,
            "exchange": source,
            "signal": signal,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "market_type": market_type,
            "trade_type": trade_type,
            "df_15m": df_15m
        })

    signals = sorted(signals, key=lambda x: x['rr'], reverse=True)[:5]

    for s in signals:

        if not is_new_signal(s['pair'], s['signal'], s['entry']):
            continue

        msg = f"""
🚀 ELITE SIGNAL

Pair: {s['pair']}
Signal: {s['signal']}
Entry: {round(s['entry'],6)}
SL: {round(s['sl'],6)}
TP: {round(s['tp'],6)}
RR: {s['rr']}
Trade Type: {s['trade_type']}
"""

        print(msg)
        send_telegram("🚀 SIGNAL (waiting for entry)\n" + msg)

        if entry_hit(s['df_15m'], s['entry'], s['signal'], s['trade_type']):

            print(f"✅ ENTRY HIT: {s['pair']}")
            send_telegram("✅ ENTRY HIT\n" + msg)

            save_trade(
                s['pair'],
                s['signal'],
                s['entry'],
                s['sl'],
                s['tp'],
                s['rr'],
                s['market_type']
            )
        else:
            print(f"⏳ Waiting for entry: {s['pair']}")

# ==============================
# LOOP
# ==============================
def main():

    ensure_csv()
    print("📁 CSV FILE LOCATION:", os.path.join(os.getcwd(), "performance.csv"))

    last_report_day = None

    while True:
        run_bot()

        check_trade_results(
            get_price,
            send_telegram
        )

        today = datetime.now().date()
        if last_report_day != today:
            daily_report(send_telegram)
            send_csv(TOKEN, CHAT_ID)
            last_report_day = today

        print("⏳ Sleeping for 15 minutes...")
        time.sleep(900)
        print("🔄 Next scan starting...")

# ==============================
# START
# ==============================
if __name__ == "__main__":
    main()