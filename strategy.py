import pandas as pd

# ==============================
# INDICATORS
# ==============================
def apply_indicators(df):
    df['ema50'] = df['close'].ewm(span=50).mean()
    df['ema200'] = df['close'].ewm(span=200).mean()
    df['rsi'] = 100 - (100 / (1 + df['close'].pct_change().rolling(14).mean()))
    return df

# ==============================
# TREND (MULTI TF)
# ==============================
def get_trend(df):
    last = df.iloc[-1]

    if last['close'] > last['ema50'] > last['ema200'] and last['rsi'] > 55:
        return "bullish"
    elif last['close'] < last['ema50'] < last['ema200'] and last['rsi'] < 45:
        return "bearish"
    return "neutral"

# ==============================
# MOMENTUM
# ==============================
def strong_momentum(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    return (
        (last['close'] > prev['close'] and last['volume'] > prev['volume']) or
        (last['close'] < prev['close'] and last['volume'] > prev['volume'])
    )

# ==============================
# SIGNAL
# ==============================
def generate_signal(df):
    last = df.iloc[-1]
    entry = last['close']

    bullish = last['close'] > last['ema50'] and last['rsi'] > 55
    bearish = last['close'] < last['ema50'] and last['rsi'] < 45

    if bullish:
        sl = entry * 0.97
        tp = entry * 1.10
        rr = (tp - entry) / (entry - sl)
        signal = "BUY"

    elif bearish:
        sl = entry * 1.03
        tp = entry * 0.90
        rr = (entry - tp) / (sl - entry)
        signal = "SELL"

    else:
        return None

    rr = round(rr, 2)

    if rr < 3:
        return None

    return signal, entry, sl, tp, rr