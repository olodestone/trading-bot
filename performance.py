"""
Stats-only module — intentionally NOT imported at bot startup.

Bot lazy-imports this file once per day (daily_report) and on /stats command.
Pandas loads into the main process only then, and stays resident, which is
acceptable for once-daily use.

All hot-path DB operations (save_trade, check_trade_results, pending trades,
compounded balance, daily loss count) live in db.py without pandas.
"""
import pandas as pd
from datetime import datetime
from db import get_engine, TRADES_TABLE, PENDING_TABLE


def _fmt(p):
    if p >= 1000:   return f"{p:,.2f}"
    if p >= 1:      return f"{p:.4f}"
    if p >= 0.01:   return f"{p:.6f}"
    if p >= 0.0001: return f"{p:.8f}"
    return f"{p:.10f}"


def _expectancy(wins, be_wins, losses, avg_win_rr, avg_be_rr=0.0):
    total = wins + be_wins + losses
    if total == 0:
        return 0.0
    return round((wins * avg_win_rr + be_wins * avg_be_rr - losses * 1.0) / total, 3)


def _mae_mfe_section(df_closed):
    if df_closed.empty or "mfe" not in df_closed.columns:
        return ""
    df = df_closed.dropna(subset=["mfe", "mae"])
    if df.empty:
        return ""
    wins   = df[df["status"] == "WIN"]
    be     = df[df["status"] == "BE_WIN"]
    losses = df[df["status"] == "LOSS"]
    lines  = ["\nMAE/MFE:"]

    def _ttf(sub, col):
        return sub[col].dropna().mean() if col in sub.columns and not sub[col].dropna().empty else 0.0

    if not wins.empty:
        lines.append(
            f"  Wins ({len(wins)}):  avg MFE {wins['mfe'].mean():.2f}R  "
            f"peak @{_ttf(wins, 'time_to_mfe'):.1f}h"
        )
    if not be.empty:
        lines.append(
            f"  BE   ({len(be)}):  avg MFE {be['mfe'].mean():.2f}R  "
            f"peak @{_ttf(be, 'time_to_mfe'):.1f}h  <- exits too early?"
        )
    if not losses.empty:
        lines.append(
            f"  Loss ({len(losses)}):  avg MFE {losses['mfe'].mean():.2f}R  "
            f"avg MAE {losses['mae'].mean():.2f}R  "
            f"SL hit @{_ttf(losses, 'time_to_mae'):.1f}h"
        )
    return "\n".join(lines) if len(lines) > 1 else ""


# ──────────────────────────────────────────────────────────────────────────────
# STATS  (pandas-heavy — only called on /stats command)
# ──────────────────────────────────────────────────────────────────────────────
def get_stats_summary():
    try:
        engine = get_engine()
        df = pd.read_sql(f"SELECT * FROM {TRADES_TABLE}", engine)
    except Exception as e:
        return f"Stats error: {e}"

    if df.empty:
        return "STATS\n\nNo closed trades yet."

    wins       = len(df[df["status"] == "WIN"])
    be_wins    = len(df[df["status"] == "BE_WIN"])
    losses     = len(df[df["status"] == "LOSS"])
    total      = wins + be_wins + losses
    if total == 0:
        return "STATS\n\nNo closed trades yet."

    win_rate  = (wins + be_wins) / total * 100
    avg_rr    = df[df["status"] == "WIN"]["rr"].mean() if wins > 0 else 0.0
    best_rr   = df[df["status"] == "WIN"]["rr"].max()  if wins > 0 else 0.0
    avg_be_rr = (df[df["status"] == "BE_WIN"]["rr"] * 0.5).mean() if be_wins > 0 else 0.0
    exp       = _expectancy(wins, be_wins, losses, avg_rr, avg_be_rr)

    closed_df = df[df["status"].isin(["WIN", "BE_WIN", "LOSS"])].copy()
    closed_df["time"] = pd.to_datetime(closed_df["time"], errors="coerce")
    closed_df = closed_df.sort_values("time")
    results = closed_df["status"].apply(
        lambda s: "W" if s in ("WIN", "BE_WIN") else "L"
    ).tolist()

    cur_streak = 1 if results else 0
    for i in range(len(results) - 1, 0, -1):
        if results[i] == results[i - 1]:
            cur_streak += 1
        else:
            break
    streak_label = (f"{cur_streak}W" if results and results[-1] == "W"
                    else f"{cur_streak}L" if results else "—")

    edge_note = "Positive edge" if exp > 0 else "No edge yet -- keep tracking"

    conf_section = ""
    if "confidence" in df.columns:
        conf_df = df[df["status"].isin(["WIN", "BE_WIN", "LOSS"]) &
                     df["confidence"].notna()].copy()
        conf_df["confidence"] = conf_df["confidence"].astype(int)
        if len(conf_df) >= 5:
            buckets = [("80-100", 80, 100), ("65-79", 65, 79),
                       ("50-64", 50, 64), ("35-49", 35, 49), ("0-34", 0, 34)]
            lines = []
            for label, lo, hi in buckets:
                sub = conf_df[(conf_df["confidence"] >= lo) & (conf_df["confidence"] <= hi)]
                if len(sub) == 0:
                    continue
                sw = len(sub[sub["status"] == "WIN"])
                sb = len(sub[sub["status"] == "BE_WIN"])
                sl = len(sub[sub["status"] == "LOSS"])
                swr    = (sw + sb) / len(sub) * 100
                s_rr   = sub[sub["status"] == "WIN"]["rr"].mean() if sw > 0 else 0.0
                s_be   = (sub[sub["status"] == "BE_WIN"]["rr"] * 0.5).mean() if sb > 0 else 0.0
                s_exp  = _expectancy(sw, sb, sl, s_rr, s_be)
                lines.append(f"  {label}  WR {swr:.0f}%  {s_exp:+.2f}R  ({len(sub)})")
            if lines:
                conf_section = f"\n{'─'*22}\nBy confidence:\n" + "\n".join(lines)

    return (
        f"ALL-TIME STATS\n{'─'*22}\n"
        f"Trades   {total} closed\n"
        f"W: {wins}  BE: {be_wins}  L: {losses}\n"
        f"Win Rate {win_rate:.1f}%\n"
        f"Avg RR   {avg_rr:.2f}\n"
        f"Best RR  {best_rr:.2f}\n{'─'*22}\n"
        f"Expectancy  {exp:+.3f}R\n"
        f"{edge_note}\n"
        f"Streak   {streak_label}\n{'─'*22}\n"
        f"Need 30+ trades for reliable stats."
        f"{conf_section}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# DAILY REPORT  (pandas-heavy — only called once per day)
# ──────────────────────────────────────────────────────────────────────────────
def daily_report(send_telegram):
    engine = get_engine()
    df = pd.read_sql(f"SELECT * FROM {TRADES_TABLE}", engine)

    if df.empty:
        send_telegram("DAILY REPORT\n\nNo trades yet.")
        return

    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    today      = datetime.utcnow().date()
    df_today   = df[df["time"].dt.date == today]

    wins    = len(df_today[df_today["status"] == "WIN"])
    be_wins = len(df_today[df_today["status"] == "BE_WIN"])
    losses  = len(df_today[df_today["status"] == "LOSS"])
    open_t  = len(df_today[df_today["status"] == "OPEN"])
    closed  = wins + be_wins + losses
    winrate = ((wins + be_wins) / closed * 100) if closed > 0 else 0
    avg_rr  = df_today[df_today["status"] == "WIN"]["rr"].mean() if wins > 0 else 0.0

    all_wins = len(df[df["status"] == "WIN"])
    all_be   = len(df[df["status"] == "BE_WIN"])
    all_loss = len(df[df["status"] == "LOSS"])
    all_cl   = all_wins + all_be + all_loss
    all_wr   = ((all_wins + all_be) / all_cl * 100) if all_cl > 0 else 0
    all_rr   = df[df["status"] == "WIN"]["rr"].mean()       if all_wins > 0 else 0.0
    all_be_rr= (df[df["status"] == "BE_WIN"]["rr"] * 0.5).mean() if all_be > 0 else 0.0
    all_exp  = _expectancy(all_wins, all_be, all_loss, all_rr, all_be_rr)

    df_closed_today = df_today[df_today["status"].isin(["WIN", "BE_WIN", "LOSS"])]
    mae_mfe = _mae_mfe_section(df_closed_today)

    send_telegram(
        f"DAILY REPORT ({today})\n\n"
        f"Today:\n"
        f"  Open: {open_t} | Closed: {closed}\n"
        f"  W: {wins}  BE: {be_wins}  L: {losses}\n"
        f"  Win Rate: {round(winrate,1)}%\n"
        f"  Avg RR (Wins): {round(avg_rr,2)}{mae_mfe}\n\n"
        f"All-Time:\n"
        f"  W: {all_wins}  BE: {all_be}  L: {all_loss}\n"
        f"  Win Rate: {round(all_wr,1)}%\n"
        f"  Expectancy: {all_exp:+.3f}R  "
        f"{'edge' if all_exp > 0 else 'no edge yet'}"
    )
