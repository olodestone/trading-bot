import pandas as pd

# ==============================
# INDICATORS
# ==============================
def apply_indicators(df):
    df['ema50'] = df['close'].ewm(span=50).mean()
    df['ema200'] = df['close'].ewm(span=200).mean()

    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    return df


# ==============================
# TREND
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
# VOLUME FILTER (IMPORTANT 🔥)
# ==============================
def high_volume(df):
    volume_ma = df['volume'].rolling(20).mean()
    last = df.iloc[-1]

    return last['volume'] > (1.3 * volume_ma.iloc[-1])  # slightly relaxed


# ==============================
# MARKET STRUCTURE
# ==============================
def get_structure_levels(df):
    recent = df.tail(20)

    swing_high = recent['high'].max()
    swing_low = recent['low'].min()

    return swing_high, swing_low


# ==============================
# SIGNAL GENERATION (FINAL 🔥)
# ==============================
def generate_signal(df):
    last = df.iloc[-1]

    swing_high, swing_low = get_structure_levels(df)

    bullish = last['close'] > last['ema50'] > last['ema200'] and last['rsi'] > 55
    bearish = last['close'] < last['ema50'] < last['ema200'] and last['rsi'] < 45

    momentum = strong_momentum(df)
    volume_ok = high_volume(df)

    # =======================
    # BUY SETUP
    # =======================
    if bullish and momentum and volume_ok:

        # 🔥 Better entry (pullback style)
        entry = (last['close'] + last['ema50']) / 2

        sl = swing_low

        # 🔥 Slight TP extension
        tp = swing_high * 1.005

        risk = entry - sl
        reward = tp - entry

        if risk <= 0 or reward <= 0:
            return None

        rr = round(reward / risk, 2)

        # 🔥 Avoid useless trades
        if rr < 1:
            return None

        signal = "BUY"

    # =======================
    # SELL SETUP
    # =======================
    elif bearish and momentum and volume_ok:

        # 🔥 Better entry (pullback style)
        entry = (last['close'] + last['ema50']) / 2

        sl = swing_high

        # 🔥 Slight TP extension
        tp = swing_low * 0.995

        risk = sl - entry
        reward = entry - tp

        if risk <= 0 or reward <= 0:
            return None

        rr = round(reward / risk, 2)

        # 🔥 Avoid useless trades
        if rr < 1:
            return None

        signal = "SELL"

    else:
        return None

    return signal, entry, sl, tp, rr