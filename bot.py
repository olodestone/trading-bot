import ccxt
import pandas as pd
import time
import requests
from datetime import datetime

from strategy import apply_indicators, get_trend, strong_momentum, generate_signal
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
        requests.post(url, data={
            "chat_id": CHAT_ID,
            "text": msg
        })
    except Exception as e:
        print("Telegram error:", e)

    if not TOKEN or not CHAT_ID:
     print("❌ Missing Telegram credentials")
    return

    send_telegram("✅ Bot is connected")

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
        trends = []

        for tf in ["15m","1h","4h","1d"]:
            df, source = fetch_tf(symbol, tf)
            if df is None:
                break

            df = apply_indicators(df)
            trends.append(get_trend(df))

        if len(trends) < 4:
            continue

        if trends.count("bullish") >= 3:
            direction = "bullish"
        elif trends.count("bearish") >= 3:
            direction = "bearish"
        else:
            continue

        df_entry, source = fetch_tf(symbol, "15m")
        if df_entry is None:
            continue

        df_entry = apply_indicators(df_entry)

        

        result = generate_signal(df_entry)
        if not result:
            continue

        signal, entry, sl, tp, rr = result

        if direction == "bullish" and signal != "BUY":
            continue
        if direction == "bearish" and signal != "SELL":
            continue

        signals.append({
            "pair": symbol,
            "exchange": source,
            "signal": signal,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr
        })

    # 🔥 SORT + LIMIT
    signals = sorted(signals, key=lambda x: x['rr'], reverse=True)[:5]

    # 🔥 THIS MUST BE INSIDE THE FUNCTION
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


