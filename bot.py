import ccxt
import pandas as pd
import time
import requests
from datetime import datetime
import os

from strategy import apply_indicators, generate_filtered_signal
from performance import save_trade, check_trade_results, daily_report
from performance import save_trade, check_trade_results, daily_report, ensure_csv

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
spot_exchange = ccxt.kucoin({"enableRateLimit": True})
futures_exchange = ccxt.mexc({"enableRateLimit": True})

# ✅ LOAD MARKETS ONCE (IMPORTANT)
SPOT_MARKETS = spot_exchange.load_markets()
FUTURES_MARKETS = futures_exchange.load_markets()

# ==============================
# FETCH DATA
# ==============================
def fetch_tf(symbol, tf, market_type):
    try:
        ex = spot_exchange if market_type == "spot" else futures_exchange
        data = ex.fetch_ohlcv(symbol, tf, limit=100)
        df = pd.DataFrame(data, columns=['time','open','high','low','close','volume'])
        return df, ex.id
    except Exception as e:
        print(f"Fetch error {symbol} {tf}: {e}")
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
# ENTRY CHECK (FIXED POSITION)
# ==============================
def entry_hit(symbol, market_type, entry, direction):
    price = get_price(symbol, market_type)

    if price is None:
        return False

    tolerance = 0.002

    if direction == "BUY":
        return price <= entry * (1 + tolerance)

    elif direction == "SELL":
        return price >= entry * (1 - tolerance)

    return False

# ==============================
# GET PAIRS (OPTIMIZED)
# ==============================
def get_pairs():
    pairs = []

    try:
        for symbol in SPOT_MARKETS:
            if "/USDT" in symbol and ":" not in symbol:

                df, _ = fetch_tf(symbol, "1h", "spot")
                time.sleep(0.2)

                if df is None or df.empty:
                    continue

                if df['volume'].iloc[-1] > 1000:
                    pairs.append((symbol, "spot"))

    except Exception as e:
        print("Spot error:", e)

    try:
        for symbol in FUTURES_MARKETS:
            if "/USDT:USDT" in symbol:

                df, _ = fetch_tf(symbol, "1h", "futures")
                time.sleep(0.5)

                if df is None or df.empty:
                    continue

                if df['volume'].iloc[-1] > 1000:
                    pairs.append((symbol, "futures"))

    except Exception as e:
        print("Futures error:", e)

    return pairs[:12]

# ==============================
# MAIN SCAN
# ==============================
def run_bot():

    print(f"\n🚀 Scan: {datetime.now()}\n")

    pairs = get_pairs()
    signals = []

    for symbol, market_type in pairs:
        time.sleep(0.3)

        df_15m, source = fetch_tf(symbol, "15m", market_type)
        df_1h, _ = fetch_tf(symbol, "1h", market_type)
        df_4h, _ = fetch_tf(symbol, "4h", market_type)
        df_1d, _ = fetch_tf(symbol, "1d", market_type)

        if any(x is None or x.empty for x in [df_15m, df_1h, df_4h, df_1d]):
            continue

        df_15m = apply_indicators(df_15m)
        df_1h = apply_indicators(df_1h)
        df_4h = apply_indicators(df_4h)
        df_1d = apply_indicators(df_1d)

        result = generate_filtered_signal(df_15m, df_1h, df_4h, df_1d)

        if not result:
            continue

        signal, entry, sl, tp, rr = result

        signals.append({
            "pair": symbol,
            "exchange": source,
            "signal": signal,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "market_type": market_type
        })

    signals = sorted(signals, key=lambda x: x['rr'], reverse=True)[:5]

    for s in signals:
        msg = f"""
🚀 ELITE SIGNAL

Pair: {s['pair']}
Signal: {s['signal']}

Entry: {round(s['entry'],4)}
SL: {round(s['sl'],4)}
TP: {round(s['tp'],4)}
RR: {s['rr']}
"""
        print(msg)

# 🔥 ALWAYS SEND SIGNAL (NEW)
        send_telegram("🚀 SIGNAL (waiting for entry)\n" + msg)

# THEN check entry
        if entry_hit(s['pair'], s['market_type'], s['entry'], s['signal']):

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
            last_report_day = today

        time.sleep(900)

# ==============================
# START
# ==============================
if __name__ == "__main__":
    main()