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
    if len(df) < 2:
        return "neutral"

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
    if len(df) < 2:
        return False

    last = df.iloc[-1]
    prev = df.iloc[-2]

    return (
        (last['close'] > prev['close'] and last['volume'] > prev['volume']) or
        (last['close'] < prev['close'] and last['volume'] > prev['volume'])
    )


# ==============================
# VOLUME FILTER
# ==============================
def high_volume(df):
    volume_ma = df['volume'].rolling(20).mean()

    if len(df) < 20 or pd.isna(volume_ma.iloc[-1]) or volume_ma.iloc[-1] == 0:
        return False

    last = df.iloc[-1]
    return last['volume'] > (1.3 * volume_ma.iloc[-1])


# ==============================
# REVERSAL DETECTION
# ==============================
def reversal_signal(df):
    if len(df) < 20:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # Stronger RSI thresholds
    overbought = last['rsi'] > 72
    oversold = last['rsi'] < 28

    # Structure break
    bearish_break = last['close'] < prev['low']
    bullish_break = last['close'] > prev['high']

    # Volume spike
    volume_ma = df['volume'].rolling(20).mean()
    if pd.isna(volume_ma.iloc[-1]) or volume_ma.iloc[-1] == 0:
        return None

    high_vol = last['volume'] > (1.5 * volume_ma.iloc[-1])

    if overbought and bearish_break and high_vol:
        return "SELL"

    if oversold and bullish_break and high_vol:
        return "BUY"

    return None


# ==============================
# MARKET STRUCTURE
# ==============================
def get_structure_levels(df):
    if len(df) < 20:
        return None, None

    recent = df.tail(20)
    return recent['high'].max(), recent['low'].min()


# ==============================
# SIGNAL GENERATION
# ==============================
def generate_signal(df):

    if len(df) < 50:
        return None

    last = df.iloc[-1]
    swing_high, swing_low = get_structure_levels(df)

    if swing_high is None or swing_low is None:
        return None

    # =======================
    # RANGE FILTER (avoid low RR zones)
    # =======================
    range_size = (swing_high - swing_low) / last['close']
    if range_size < 0.01:
        return None

    # =======================
    # REVERSAL FIRST
    # =======================
    rev = reversal_signal(df)

    if rev:
        entry = last['close']

        if rev == "BUY":
            sl = swing_low
            tp = swing_high
            risk = entry - sl
            reward = tp - entry
        else:
            sl = swing_high
            tp = swing_low
            risk = sl - entry
            reward = entry - tp

        if risk <= 0 or reward <= 0:
            return None

        rr = round(reward / risk, 2)

        if rr < 1:
            return None

        return f"{rev}_REVERSAL", entry, sl, tp, rr

    # =======================
    # TREND LOGIC (UNCHANGED CORE)
    # =======================
    bullish = last['close'] > last['ema50'] > last['ema200'] and last['rsi'] > 55
    bearish = last['close'] < last['ema50'] < last['ema200'] and last['rsi'] < 45

    momentum = strong_momentum(df)
    volume_ok = high_volume(df)

    # =======================
    # BUY
    # =======================
    if bullish and momentum and volume_ok:

        entry = (last['close'] + last['ema50']) / 2
        sl = swing_low
        tp = swing_high * 1.005

        risk = entry - sl
        reward = tp - entry

        if risk <= 0 or reward <= 0:
            return None

        rr = round(reward / risk, 2)

        if rr < 1:
            return None

        return "BUY", entry, sl, tp, rr

    # =======================
    # SELL
    # =======================
    elif bearish and momentum and volume_ok:

        entry = (last['close'] + last['ema50']) / 2
        sl = swing_high
        tp = swing_low * 0.995

        risk = sl - entry
        reward = entry - tp

        if risk <= 0 or reward <= 0:
            return None

        rr = round(reward / risk, 2)

        if rr < 1:
            return None

        return "SELL", entry, sl, tp, rr

    return None