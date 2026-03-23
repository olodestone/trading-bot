import pandas as pd
import csv
import os
from datetime import datetime

CSV_FILE = "performance.csv"

# ==============================
# INIT FILE
# ==============================
if not os.path.exists(CSV_FILE):
    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["time","pair","signal","entry","sl","tp","rr","status"])

# ==============================
# SAVE TRADE
# ==============================
def save_trade(pair, signal, entry, sl, tp, rr):
    with open(CSV_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.utcnow(), pair, signal, entry, sl, tp, rr, "OPEN"
        ])

# ==============================
# TP/SL CHECK
# ==============================
def check_trade_results(fetch_price_func, send_telegram):
    df = pd.read_csv(CSV_FILE)
    updated = False

    for i, row in df.iterrows():
        if row['status'] != "OPEN":
            continue

        price = fetch_price_func(row['pair'])
        if price is None:
            continue

        if row['signal'] == "BUY":
            if price >= row['tp']:
                df.at[i, 'status'] = "WIN"
                send_telegram(f"✅ TP HIT: {row['pair']}")
                updated = True

            elif price <= row['sl']:
                df.at[i, 'status'] = "LOSS"
                send_telegram(f"❌ SL HIT: {row['pair']}")
                updated = True

        else:
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
# DAILY REPORT (FIXED)
# ==============================
def daily_report(send_telegram):
    df = pd.read_csv(CSV_FILE)

    df['time'] = pd.to_datetime(df['time'])
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