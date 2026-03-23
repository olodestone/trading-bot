import ccxt
import pandas as pd
import time
import requests
from datetime import datetime

# ✅ UPDATED IMPORT
from strategy import apply_indicators, generate_filtered_signal
from performance import save_trade, check_trade_results, daily_report

# ==============================
# TELEGRAM
# ==============================
import os

TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_telegram(msg):
    if not TOKEN or not CHAT_ID:
        print("❌ Missing Telegram credentials")
        return

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    try:
        response = requests.post(url, data={
            "chat_id": CHAT_ID,
            "text": msg
        })

        if response.status_code != 200:
            print("❌ Telegram failed:", response.text)

    except Exception as e:
        print("Telegram error:", e)

send_telegram("✅ Bot started successfully")

# ==============================
# EXCHANGES
# ==============================
exchanges = {
    "kucoin": ccxt.kucoin({"enableRateLimit": True}),
    "mexc": ccxt.mexc({"enableRateLimit": True})
}

# ==============================
# FETCH DATA
# ==============================
def fetch_tf(symbol, tf):
    for name, ex in exchanges.items():
        try:
            data = ex.fetch_ohlcv(symbol, tf, limit=100)
            df = pd.DataFrame(data, columns=['time','open','high','low','close','volume'])
            return df, name
        except:
            continue
    return None, None

def get_price(symbol):
    df, _ = fetch_tf(symbol, "15m")
    if df is None:
        return None
    return df.iloc[-1]['close']

# ==============================
# PAIR SCANNER
# ==============================
def get_pairs():
    pairs = []

    for ex in exchanges.values():
        try:
            markets = ex.load_markets()

            for symbol, data in markets.items():
                if "/USDT" in symbol:
                    try:
                        ticker = ex.fetch_ticker(symbol)

                        # 🔥 Only high volume coins
                        if ticker['quoteVolume'] and ticker['quoteVolume'] > 5_000_000:
                            pairs.append(symbol)

                    except:
                        continue

        except:
            continue

    return list(set(pairs))[:25]

# ==============================
# MAIN BOT
# ==============================
def run_bot():
    print(f"\n🚀 Scan: {datetime.now()}\n")

    pairs = get_pairs()
    signals = []

    for symbol in pairs:

        # ==============================
        # FETCH ALL TIMEFRAMES
        # ==============================
        df_15m, source = fetch_tf(symbol, "15m")
        df_1h, _ = fetch_tf(symbol, "1h")
        df_4h, _ = fetch_tf(symbol, "4h")
        df_1d, _ = fetch_tf(symbol, "1d")

        if None in [df_15m, df_1h, df_4h, df_1d]:
            continue

        # Apply indicators
        df_15m = apply_indicators(df_15m)
        df_1h = apply_indicators(df_1h)
        df_4h = apply_indicators(df_4h)
        df_1d = apply_indicators(df_1d)

        # ==============================
        # NEW HTF FILTERED SIGNAL
        # ==============================
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
            "rr": rr
        })

    # ==============================
    # SORT + LIMIT
    # ==============================
    signals = sorted(signals, key=lambda x: x['rr'], reverse=True)[:5]

    for s in signals:
        msg = f"""
==============================

🚀 ELITE SIGNAL

Pair: {s['pair']}
Exchange: {s['exchange']}
Signal: {s['signal']}

Entry: {round(s['entry'],4)}
SL: {round(s['sl'],4)}
TP: {round(s['tp'],4)}

RR: {s['rr']}

==============================
"""
        print(msg)
        send_telegram(msg)

        save_trade(s['pair'], s['signal'], s['entry'], s['sl'], s['tp'], s['rr'])

        time.sleep(0.5)

# ==============================
# LOOP
# ==============================
last_report_day = None

while True:
    run_bot()

    check_trade_results(get_price, send_telegram)

    today = datetime.now().date()
    if last_report_day != today:
        daily_report(send_telegram)
        last_report_day = today

    time.sleep(900)