import os
import io
import requests

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
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)


# ==============================
# SEND TRADES CSV TO TELEGRAM
# ==============================
def send_csv(token, chat_id):
    if not token or not chat_id:
        return
    try:
        from performance import get_engine, TRADES_TABLE
        import pandas as pd
        engine = get_engine()
        df = pd.read_sql(f"SELECT * FROM {TRADES_TABLE}", engine)
        buf = io.BytesIO()
        df.to_csv(buf, index=False)
        buf.seek(0)
        url = f"https://api.telegram.org/bot{token}/sendDocument"
        requests.post(
            url,
            files={"document": ("trades.csv", buf, "text/csv")},
            data={"chat_id": chat_id},
            timeout=15,
        )
        print("📁 CSV sent to Telegram")
    except Exception as e:
        print("send_csv error:", e)
