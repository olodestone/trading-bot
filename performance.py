"""
Stats-only module — intentionally NOT imported at bot startup.

Bot lazy-imports this file once per day (daily_report) and on /stats command.
Pandas loads into the main process only then, and stays resident, which is
acceptable for once-daily use.

All hot-path DB operations (save_trade, check_trade_results, pending trades,
compounded balance, daily loss count) live in db.py without pandas.
"""
import os
import re
import pandas as pd
from datetime import datetime
from db import get_engine, TRADES_TABLE, PENDING_TABLE


def _current_plan() -> int | None:
    """Return highest Plan N from CLAUDE.md, or None on failure."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "CLAUDE.md")
        with open(path) as f:
            nums = re.findall(r"^## Plan (\d+)", f.read(), re.MULTILINE)
        return max(int(n) for n in nums) if nums else None
    except Exception:
        return None


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

    all_closed = df[df["status"].isin(["WIN", "BE_WIN", "LOSS"])].copy()
    if all_closed.empty:
        return "STATS\n\nNo closed trades yet."

    # Headline uses current plan only; falls back to all-time if no plan data
    cur_plan = _current_plan()
    if cur_plan is not None and "plan" in df.columns:
        headline_df = all_closed[all_closed["plan"] == cur_plan]
        plan_label  = f"PLAN {cur_plan} STATS"
    else:
        headline_df = all_closed
        plan_label  = "ALL-TIME STATS"

    if headline_df.empty:
        # New plan just started — show waiting message + historical by-plan below
        history_section = _plan_breakdown(all_closed, cur_plan)
        return (
            f"{plan_label}\n{'─'*22}\n"
            f"No closed trades yet — watching for Plan {cur_plan} results.\n"
            f"{history_section}"
        )

    wins       = len(headline_df[headline_df["status"] == "WIN"])
    be_wins    = len(headline_df[headline_df["status"] == "BE_WIN"])
    losses     = len(headline_df[headline_df["status"] == "LOSS"])
    total      = wins + be_wins + losses
    win_rate   = (wins + be_wins) / total * 100
    avg_rr     = headline_df[headline_df["status"] == "WIN"]["rr"].mean() if wins > 0 else 0.0
    best_rr    = headline_df[headline_df["status"] == "WIN"]["rr"].max()  if wins > 0 else 0.0
    avg_be_rr  = (headline_df[headline_df["status"] == "BE_WIN"]["rr"] * 0.5).mean() if be_wins > 0 else 0.0
    exp        = _expectancy(wins, be_wins, losses, avg_rr, avg_be_rr)

    headline_df = headline_df.sort_values(
        pd.to_datetime(headline_df["time"], errors="coerce")
    ) if "time" in headline_df.columns else headline_df
    results = headline_df["status"].apply(
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
    sample_note = "" if total >= 30 else f"\n⚠ {total}/30 trades — early sample"

    conf_section = ""
    if "confidence" in headline_df.columns:
        conf_df = headline_df[headline_df["confidence"].notna()].copy()
        conf_df["confidence"] = conf_df["confidence"].astype(int)
        if len(conf_df) >= 5:
            buckets = [("80-100", 80, 100), ("65-79", 65, 79),
                       ("50-64", 50, 64), ("35-49", 35, 49), ("0-34", 0, 34)]
            lines = []
            for label, lo, hi in buckets:
                sub = conf_df[(conf_df["confidence"] >= lo) & (conf_df["confidence"] <= hi)]
                if len(sub) == 0:
                    continue
                sw     = len(sub[sub["status"] == "WIN"])
                sb     = len(sub[sub["status"] == "BE_WIN"])
                sl     = len(sub[sub["status"] == "LOSS"])
                swr    = (sw + sb) / len(sub) * 100
                s_rr   = sub[sub["status"] == "WIN"]["rr"].mean() if sw > 0 else 0.0
                s_be   = (sub[sub["status"] == "BE_WIN"]["rr"] * 0.5).mean() if sb > 0 else 0.0
                s_exp  = _expectancy(sw, sb, sl, s_rr, s_be)
                lines.append(f"  {label}  WR {swr:.0f}%  {s_exp:+.2f}R  ({len(sub)})")
            if lines:
                conf_section = f"\n{'─'*22}\nBy confidence:\n" + "\n".join(lines)

    history_section = _plan_breakdown(all_closed, cur_plan)

    return (
        f"{plan_label}\n{'─'*22}\n"
        f"Trades   {total} closed\n"
        f"W: {wins}  BE: {be_wins}  L: {losses}\n"
        f"Win Rate {win_rate:.1f}%\n"
        f"Avg RR   {avg_rr:.2f}\n"
        f"Best RR  {best_rr:.2f}\n{'─'*22}\n"
        f"Expectancy  {exp:+.3f}R\n"
        f"{edge_note}\n"
        f"Streak   {streak_label}"
        f"{sample_note}"
        f"{conf_section}"
        f"{history_section}"
    )


def _plan_breakdown(closed_df: "pd.DataFrame", cur_plan: int | None) -> str:
    """Return a 'By plan' section string, always showing all plans."""
    if "plan" not in closed_df.columns or closed_df.empty:
        return ""
    labelled = closed_df.copy()
    # Determine the label for NULL plan rows (pre-current-plan history)
    null_label = f"pre-{cur_plan}" if cur_plan is not None else "pre-plan"
    labelled["plan_label"] = labelled["plan"].apply(
        lambda p: f"Plan {int(p)}" if pd.notna(p) else null_label
    )
    unique = sorted(labelled["plan_label"].unique())
    if len(unique) <= 1 and cur_plan is not None:
        # Only one plan exists — no breakdown adds value yet
        return ""
    lines = []
    for label in unique:
        sub = labelled[labelled["plan_label"] == label]
        sw   = len(sub[sub["status"] == "WIN"])
        sb   = len(sub[sub["status"] == "BE_WIN"])
        sl_  = len(sub[sub["status"] == "LOSS"])
        swr  = (sw + sb) / len(sub) * 100
        s_rr = sub[sub["status"] == "WIN"]["rr"].mean() if sw > 0 else 0.0
        s_be = (sub[sub["status"] == "BE_WIN"]["rr"] * 0.5).mean() if sb > 0 else 0.0
        s_exp = _expectancy(sw, sb, sl_, s_rr, s_be)
        lines.append(
            f"  {label:<10}  W{sw} BE{sb} L{sl_}  "
            f"WR {swr:.0f}%  {s_exp:+.2f}R"
        )
    return f"\n{'─'*22}\nBy plan:\n" + "\n".join(lines)


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
