import ccxt
import pandas as pd
import time
import requests
from datetime import datetime
import os

# ==============================
# IMPORT STRATEGY
# ==============================
from strategy import apply_indicators, generate_filtered_signal
from performance import save_trade, check_trade_results, daily_report

# ==============================
# TELEGRAM
# ==============================
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
# EXCHANGES (SEPARATED)
# ==============================
spot_exchange = ccxt.kucoin({"enableRateLimit": True})
futures_exchange = ccxt.mexc({"enableRateLimit": True})

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
        print(f"{market_type} fetch failed for {symbol}: {e}")
        return None, None

def get_price(symbol, market_type):
    df, _ = fetch_tf(symbol, "15m", market_type)
    if df is None:
        return None
    return df.iloc[-1]['close']

# ==============================
# PAIR SCANNER
# ==============================
def get_pairs():
    pairs = []

    # ===== KUCOIN SPOT =====
    try:
        markets = spot_exchange.load_markets()

        for symbol in markets:
            if "/USDT" in symbol and ":" not in symbol:
                try:
                    ticker = spot_exchange.fetch_ticker(symbol)
                    if ticker['quoteVolume'] and ticker['quoteVolume'] > 5_000_000:
                        pairs.append((symbol, "spot"))
                except:
                    continue
    except:
        pass

    # ===== MEXC FUTURES =====
    try:
        markets = futures_exchange.load_markets()

        for symbol in markets:
            if "/USDT:USDT" in symbol:
                try:
                    ticker = futures_exchange.fetch_ticker(symbol)
                    if ticker['quoteVolume'] and ticker['quoteVolume'] > 5_000_000:
                        pairs.append((symbol, "futures"))
                except:
                    continue
    except:
        pass

    return pairs[:25]

# ==============================
# MAIN BOT
# ==============================
def run_bot():
    print(f"\n🚀 Scan: {datetime.now()}\n")

    pairs = get_pairs()
    signals = []

    for symbol, market_type in pairs:

        # Fetch all TFs
        df_15m, source = fetch_tf(symbol, "15m", market_type)
        df_1h, _ = fetch_tf(symbol, "1h", market_type)
        df_4h, _ = fetch_tf(symbol, "4h", market_type)
        df_1d, _ = fetch_tf(symbol, "1d", market_type)

        # Data check
        if any(x is None or x.empty for x in [df_15m, df_1h, df_4h, df_1d]):
            print(f"⚠️ Skipping {symbol} ({market_type})")
            continue

        # Indicators
        df_15m = apply_indicators(df_15m)
        df_1h = apply_indicators(df_1h)
        df_4h = apply_indicators(df_4h)
        df_1d = apply_indicators(df_1d)

        # Strategy
        result = generate_filtered_signal(df_15m, df_1h, df_4h, df_1d)

        if not result:
            continue

        signal, entry, sl, tp, rr = result

        if entry == sl or entry == tp:
            continue

        signals.append({
            "pair": symbol,
            "exchange": source,
            "signal": signal,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr,
            "market_type": market_type   # ✅ added for correct price tracking
        })

    # Sort best trades
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

    # ✅ FIXED: now uses correct market type (spot/futures)
    check_trade_results(
        lambda s: get_price(s[0], s[1]),
        send_telegram
    )

    today = datetime.now().date()
    if last_report_day != today:
        daily_report(send_telegram)
        last_report_day = today

    time.sleep(900)