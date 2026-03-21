import time
from strategy import get_signals
from logger import log_trade
from performance import update_performance
import os
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg})

def run():
    print("🚀 Elite Bot Running...")

    while True:
        signals = get_signals()

        if signals:
            for s in signals:
                msg = f"""
🔥 ELITE TRADE

Pair: {s['pair']}
Signal: {s['signal']}
Entry: {s['entry']}
SL: {s['sl']}
TP: {s['tp']}
RR: 1:{s['rr']}
"""
                print(msg)
                send_telegram(msg)
                log_trade(s)

        update_performance()

        time.sleep(900)

if __name__ == "__main__":
    run()