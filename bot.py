import ccxt
import pandas as pd
import time
import requests
import os

# ====== CONFIG ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ====== TELEGRAM FUNCTION ======
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": message
    }
    requests.post(url, data=data)

# ====== SIMPLE MARKET SCAN ======
def scan_market():
    exchange = ccxt.binance()
    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    signals = []

    for symbol in symbols:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=50)
            df = pd.DataFrame(ohlcv, columns=['time','open','high','low','close','volume'])

            last_close = df['close'].iloc[-1]
            prev_close = df['close'].iloc[-2]

            if last_close > prev_close:
                signals.append(f"📈 BUY: {symbol}")
            else:
                signals.append(f"📉 SELL: {symbol}")

        except Exception as e:
            print(f"Error on {symbol}: {e}")

    return signals

# ====== MAIN LOOP ======
def run_bot():
    print("🚀 Bot is running...")

    while True:
        try:
            signals = scan_market()

            if signals:
                message = "🔥 Trading Signals:\n\n" + "\n".join(signals)
                print(message)
                send_telegram(message)

            time.sleep(900)  # 15 minutes

        except Exception as e:
            print("Error:", e)
            time.sleep(60)

# ====== START ======
if __name__ == "__main__":
    run_bot()