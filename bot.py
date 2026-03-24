import ccxt
import pandas as pd
import time
import requests
from datetime import datetime
import os

from strategy import apply_indicators, generate_filtered_signal
from performance import save_trade, check_trade_results, daily_report

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
# GET PRICE (USED BY PERFORMANCE)
# ==============================
def get_price(symbol, market_type):
    df, _ = fetch_tf(symbol, "15m", market_type)
    if df is None or df.empty:
        return None
    return df.iloc[-1]['close']

# ==============================
# GET PAIRS
# ==============================
def get_pairs():
    pairs = []

    try:
        for symbol in spot_exchange.load_markets():
            if "/USDT" in symbol and ":" not in symbol:
                ticker = spot_exchange.fetch_ticker(symbol)
                if ticker.get('quoteVolume') and ticker['quoteVolume'] > 5_000_000:
                    pairs.append((symbol, "spot"))
    except Exception as e:
        print("Spot error:", e)

    try:
        for symbol in futures_exchange.load_markets():
            if "/USDT:USDT" in symbol:
                ticker = futures_exchange.fetch_ticker(symbol)
                if ticker.get('quoteVolume') and ticker['quoteVolume'] > 5_000_000:
                    pairs.append((symbol, "futures"))
    except Exception as e:
        print("Futures error:", e)

    return pairs[:15]

# ==============================
# MAIN SCAN
# ==============================
def run_bot():

    print(f"\n🚀 Scan: {datetime.now()}\n")

    pairs = get_pairs()
    signals = []

    for symbol, market_type in pairs:
        time.sleep(0.3)  # prevents MEXC rate limit

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

    # Pick top RR trades
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
        send_telegram(msg)

        # ✅ FIXED (added market_type)
        save_trade(
            s['pair'],
            s['signal'],
            s['entry'],
            s['sl'],
            s['tp'],
            s['rr'],
            s['market_type']
        )

# ==============================
# LOOP
# ==============================
def main():
    last_report_day = None

    while True:
        run_bot()

        # ✅ FIXED (no lambda anymore)
        check_trade_results(
            get_price,
            send_telegram
        )

        # Better timing control (optional tweak later)
        today = datetime.now().date()
        if last_report_day != today:
            daily_report(send_telegram)
            last_report_day = today

        time.sleep(900)  # 15 minutes

# ==============================
# START
# ==============================
if __name__ == "__main__":
    main()