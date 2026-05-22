#!/usr/bin/env python3
"""
Isolated report subprocess.

Spawned by bot.py for daily_report and /stats commands.
Loads pandas + sqlalchemy here, runs the report, exits — OS reclaims all memory.
Main process never imports pandas and stays at ~42 MB permanently.

Usage:
    python report_worker.py daily   — daily_report() + send_csv()
    python report_worker.py stats   — get_stats_summary() sent to Telegram
"""
import gc, sys

mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
print(f"[report] mode={mode}", flush=True)

from performance import daily_report, get_stats_summary
from logger import send_telegram, send_csv, TOKEN, CHAT_ID

if mode == "daily":
    daily_report(send_telegram)
    send_csv(TOKEN, CHAT_ID)
elif mode == "stats":
    send_telegram(get_stats_summary())

gc.collect()
print(f"[report] done", flush=True)
