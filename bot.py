from strategy import generate_signal, strong_momentum, apply_indicators, get_trend
from performance import save_trade, check_trade_results, daily_report

def run_bot():
    pairs = get_top_pairs()
    signals = []

    for symbol in pairs:

        trends = []

        for tf in ["15m","1h","4h","1d"]:
            df, source = fetch_tf(symbol, tf)
            if df is None:
                break

            df = apply_indicators(df)
            trends.append(get_trend(df))

        if len(trends) < 4:
            continue

        # 🔥 alignment
        if trends.count("bullish") >= 3:
            direction = "bullish"
        elif trends.count("bearish") >= 3:
            direction = "bearish"
        else:
            continue

        # entry tf
        df_entry, source = fetch_tf(symbol, "15m")
        if df_entry is None:
            continue

        df_entry = apply_indicators(df_entry)

        if not strong_momentum(df_entry):
            continue

        result = generate_signal(df_entry)
        if not result:
            continue

        signal, entry, sl, tp, rr = result

        # match direction
        if direction == "bullish" and signal != "BUY":
            continue
        if direction == "bearish" and signal != "SELL":
            continue

        signals.append({
            "pair": symbol,
            "exchange": source,
            "signal": signal,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rr": rr
        })

        for s in signals:
    msg = f"""
🚀 ELITE SIGNAL

Pair: {s['pair']}
Exchange: {s['exchange']}
Signal: {s['signal']}

Entry: {round(s['entry'],4)}
SL: {round(s['sl'],4)}
TP: {round(s['tp'],4)}

RR: {s['rr']}
"""
    send_telegram(msg)

    save_trade(
        s['pair'],
        s['signal'],
        s['entry'],
        s['sl'],
        s['tp'],
        s['rr']
    )

    last_report_day = None

while True:
    run_bot()

    # 🔥 track TP/SL
    check_trade_results()

    # 📊 daily report
    today = datetime.now().date()
    if last_report_day != today:
        daily_report()
        last_report_day = today

    time.sleep(900)