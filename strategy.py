import pandas as pd

# ==============================
# INDICATORS (FIXED RSI)
# ==============================
def apply_indicators(df):
    df['ema50'] = df['close'].ewm(span=50).mean()
    df['ema200'] = df['close'].ewm(span=200).mean()

    # ✅ Proper RSI calculation
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

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
# MARKET STRUCTURE (NEW 🔥)
# ==============================
def get_structure_levels(df):
    recent = df.tail(20)

    swing_high = recent['high'].max()
    swing_low = recent['low'].min()

    return swing_high, swing_low

# ==============================
# SIGNAL (DYNAMIC RR 🔥)
# ==============================
def high_volume(df):
    volume_ma = df['volume'].rolling(20).mean()
    last = df.iloc[-1]

    return last['volume'] > (1.5 * volume_ma.iloc[-1])


def generate_signal(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    entry = last['close']
    swing_high, swing_low = get_structure_levels(df)

    bullish = last['close'] > last['ema50'] > last['ema200'] and last['rsi'] > 55
    bearish = last['close'] < last['ema50'] < last['ema200'] and last['rsi'] < 45

    momentum = strong_momentum(df)
    volume_ok = high_volume(df)

    # =======================
    # BUY
    # =======================
    if bullish and momentum and volume_ok:
        sl = swing_low
        tp = swing_high

        risk = entry - sl
        reward = tp - entry

        if risk <= 0 or reward <= 0:
            return None

        rr = round(reward / risk, 2)
        signal = "BUY"

    # =======================
    # SELL
    # =======================
    elif bearish and momentum and volume_ok:
        sl = swing_high
        tp = swing_low

        risk = sl - entry
        reward = entry - tp

        if risk <= 0 or reward <= 0:
            return None

        rr = round(reward / risk, 2)
        signal = "SELL"

    else:
        return None

    # ✅ NO MORE RR FILTER
    return signal, entry, sl, tp, rr