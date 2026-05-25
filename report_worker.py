#!/usr/bin/env python3
"""
Isolated report subprocess.

Spawned by bot.py for daily_report, /stats, and /edge commands.
Loads pandas + sqlalchemy here, runs the report, exits — OS reclaims all memory.
Main process never imports pandas and stays at ~42 MB permanently.

Usage:
    python report_worker.py daily          — daily_report() + send_csv()
    python report_worker.py stats          — get_stats_summary() sent to Telegram
    python report_worker.py edge           — full funnel + direction accuracy
    python report_worker.py edge_session   — breakdown by session
    python report_worker.py edge_regime    — breakdown by market regime
    python report_worker.py edge_dir       — breakdown by direction (BUY/SELL)
    python report_worker.py edge_conf      — breakdown by confidence band
    python report_worker.py edge_type      — breakdown by trade type
    python report_worker.py edge_pair_SYM  — pair-specific analysis
"""
import gc, sys

mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
print(f"[report] mode={mode}", flush=True)

from logger import send_telegram, send_csv, TOKEN, CHAT_ID

if mode == "daily":
    from performance import daily_report
    daily_report(send_telegram)
    send_csv(TOKEN, CHAT_ID)
elif mode == "stats":
    from performance import get_stats_summary
    send_telegram(get_stats_summary())
elif mode == "diagnose":
    from performance import get_diagnose_report
    send_telegram(get_diagnose_report())
elif mode.startswith("edge"):
    from performance import get_edge_report
    send_telegram(get_edge_report(mode))

gc.collect()
print(f"[report] done", flush=True)
