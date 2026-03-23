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

def send_telegram(msg):
    if not TOKEN or not CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})

spot_exchange = ccxt.kucoin({"enableRateLimit": True})
futures_exchange = ccxt.mexc({"enableRateLimit": True})

def fetch_tf(symbol, tf, market_type):
    try:
        ex = spot_exchange if market_type == "spot" else futures_exchange
        data = ex.fetch_ohlcv(symbol, tf, limit=100)
        df = pd.DataFrame(data, columns=['time','open','high','low','close','volume'])
        return df, ex.id
    except:
        return None, None

def get_price(symbol, market_type):
    df, _ = fetch_tf(symbol, "15m", market_type)
    if df is None:
        return None
    return df.iloc[-1]['close']

def get_pairs():
    pairs = []

    try:
        for symbol in spot_exchange.load_markets():
            if "/USDT" in symbol and ":" not in symbol:
                ticker = spot_exchange.fetch_ticker(symbol)
                if ticker['quoteVolume'] and ticker['quoteVolume'] > 5_000_000:
                    pairs.append((symbol, "spot"))
    except:
        pass

    try:
        for symbol in futures_exchange.load_markets():
            if "/USDT:USDT" in symbol:
                ticker = futures_exchange.fetch_ticker(symbol)
                if ticker['quoteVolume'] and ticker['quoteVolume'] > 5_000_000:
                    pairs.append((symbol, "futures"))
    except:
        pass

    return pairs[:25]

def run_bot():

    print(f"\n🚀 Scan: {datetime.now()}\n")

    pairs = get_pairs()
    signals = []

    for symbol, market_type in pairs:

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
        send_telegram(msg)

        save_trade(s['pair'], s['signal'], s['entry'], s['sl'], s['tp'], s['rr'])

def main():
    last_report_day = None

    while True:
        run_bot()

        check_trade_results(
            lambda s: get_price(s[0], s[1]),
            send_telegram
        )

        today = datetime.now().date()
        if last_report_day != today:
            daily_report(send_telegram)
            last_report_day = today

        time.sleep(900)

if __name__ == "__main__":
    main()