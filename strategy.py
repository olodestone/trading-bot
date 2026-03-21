import ccxt
import pandas as pd

exchange = ccxt.binance()

def get_top_pairs():
    markets = exchange.load_markets()
    pairs = []

    for symbol in markets:
        if "/USDT" in symbol and markets[symbol]['active']:
            pairs.append(symbol)

    return pairs[:50]  # top 50 pairs (safe)

def get_signals():
    signals = []
    pairs = get_top_pairs()

    for pair in pairs:
        try:
            data = exchange.fetch_ohlcv(pair, '15m', limit=50)
            df = pd.DataFrame(data, columns=['t','o','h','l','c','v'])

            # Volume filter (avoid dead coins)
            avg_vol = df['v'].mean()
            if avg_vol < 1000:
                continue

            # EMA trend
            df['ema'] = df['c'].ewm(span=20).mean()

            last = df.iloc[-1]
            prev = df.iloc[-2]

            bullish = last['c'] > last['ema'] and last['c'] > prev['c']
            bearish = last['c'] < last['ema'] and last['c'] < prev['c']

            if bullish:
                entry = last['c']
                sl = entry * 0.98
                tp = entry * 1.06
                rr = round((tp - entry) / (entry - sl), 2)

                if rr >= 3:
                    signals.append({
                        "pair": pair,
                        "signal": "BUY",
                        "entry": round(entry, 4),
                        "sl": round(sl, 4),
                        "tp": round(tp, 4),
                        "rr": rr
                    })

            elif bearish:
                entry = last['c']
                sl = entry * 1.02
                tp = entry * 0.94
                rr = round((entry - tp) / (sl - entry), 2)

                if rr >= 3:
                    signals.append({
                        "pair": pair,
                        "signal": "SELL",
                        "entry": round(entry, 4),
                        "sl": round(sl, 4),
                        "tp": round(tp, 4),
                        "rr": rr
                    })

        except Exception as e:
            print(f"{pair} error:", e)

    return signals[:5]  # only send top 5 signals