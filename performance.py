import pandas as pd
import csv
import os
from datetime import datetime

# ✅ FORCE LOCAL FILE (same folder as bot.py)
CSV_FILE = os.path.join(os.getcwd(), "performance.csv")

# ==============================
# ENSURE FILE EXISTS
# ==============================
def ensure_csv():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "time","pair","signal","entry","sl","tp","rr","status","market_type"
            ])
        print(f"✅ CSV CREATED AT: {CSV_FILE}")

# ==============================
# SAVE TRADE
# ==============================
def save_trade(pair, signal, entry, sl, tp, rr, market_type):
    ensure_csv()

    with open(CSV_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.utcnow(),
            pair,
            signal,
            entry,
            sl,
            tp,
            rr,
            "OPEN",
            market_type
        ])

# ==============================
# TP/SL CHECK
# ==============================
def check_trade_results(fetch_price_func, send_telegram):
    ensure_csv()

    df = pd.read_csv(CSV_FILE)

    if df.empty:
        return

    updated = False

    for i, row in df.iterrows():

        if row['status'] != "OPEN":
            continue

        try:
            price = fetch_price_func(row['pair'], row['market_type'])
        except Exception as e:
            print(f"Price fetch error: {row['pair']} -> {e}")
            continue

        if price is None:
            continue

        print(f"Checking {row['pair']} | Price: {price}")

        if row['signal'] == "BUY":
            if price >= row['tp']:
                df.at[i, 'status'] = "WIN"
                send_telegram(f"✅ TP HIT: {row['pair']}")
                updated = True

            elif price <= row['sl']:
                df.at[i, 'status'] = "LOSS"
                send_telegram(f"❌ SL HIT: {row['pair']}")
                updated = True

        elif row['signal'] == "SELL":
            if price <= row['tp']:
                df.at[i, 'status'] = "WIN"
                send_telegram(f"✅ TP HIT: {row['pair']}")
                updated = True

            elif price >= row['sl']:
                df.at[i, 'status'] = "LOSS"
                send_telegram(f"❌ SL HIT: {row['pair']}")
                updated = True

    if updated:
        df.to_csv(CSV_FILE, index=False)

# ==============================
# DAILY REPORT
# ==============================
def daily_report(send_telegram):
    ensure_csv()

    df = pd.read_csv(CSV_FILE)

    if df.empty:
        send_telegram("📊 DAILY REPORT\n\nNo trades yet.")
        return

    df['time'] = pd.to_datetime(df['time'], errors='coerce')
    today = datetime.utcnow().date()

    df_today = df[df['time'].dt.date == today]

    total_trades = len(df_today)
    wins = len(df_today[df_today['status'] == "WIN"])
    losses = len(df_today[df_today['status'] == "LOSS"])
    open_trades = len(df_today[df_today['status'] == "OPEN"])

    closed = wins + losses
    winrate = (wins / closed * 100) if closed > 0 else 0

    msg = f"""
📊 DAILY REPORT

Total Trades: {total_trades}
Open Trades: {open_trades}
Closed Trades: {closed}

Wins: {wins}
Losses: {losses}
Win Rate: {round(winrate,2)}%
"""

    send_telegram(msg)


import requests

def send_csv(token, chat_id):
    if not os.path.exists(CSV_FILE):
        print("CSV not found")
        return

    url = f"https://api.telegram.org/bot{token}/sendDocument"

    with open(CSV_FILE, "rb") as f:
        requests.post(
            url,
            files={"document": f},
            data={"chat_id": chat_id}
        )

    print("📁 CSV sent to Telegram")