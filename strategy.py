import pandas as pd

# ==============================
# INDICATORS
# ==============================
def apply_indicators(df):
    df['ema50'] = df['close'].ewm(span=50).mean()
    df['ema200'] = df['close'].ewm(span=200).mean()

    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    return df

# ==============================
# MARKET CONDITION (NEW)
# ==============================
def is_trending(df):
    recent = df.tail(30)
    range_size = (recent['high'].max() - recent['low'].min()) / recent['close'].iloc[-1]
    return range_size > 0.015  # avoid choppy markets

# ==============================
# STRUCTURE
# ==============================
def structure_bias(df):
    highs = df['high'].rolling(5).max()
    lows = df['low'].rolling(5).min()

    last_high = highs.iloc[-1]
    prev_high = highs.iloc[-5]

    last_low = lows.iloc[-1]
    prev_low = lows.iloc[-5]

    if last_high > prev_high and last_low > prev_low:
        return "bullish"

    if last_high < prev_high and last_low < prev_low:
        return "bearish"

    return "neutral"

# ==============================
# HTF DIRECTION
# ==============================
def get_htf_bias(df_1h, df_4h, df_1d):

    trends = [
        structure_bias(df_1h),
        structure_bias(df_4h),
        structure_bias(df_1d)
    ]

    if trends.count("bullish") >= 2:
        return "BUY"

    if trends.count("bearish") >= 2:
        return "SELL"

    return None

# ==============================
# HTF REVERSAL DETECTION
# ==============================
def detect_htf_reversal(df_4h, df_1d):

    trend_4h = structure_bias(df_4h)
    trend_1d = structure_bias(df_1d)

    # weakening condition
    if trend_1d == "bullish" and trend_4h == "bearish":
        return "SELL"

    if trend_1d == "bearish" and trend_4h == "bullish":
        return "BUY"

    return None

# ==============================
# ENTRY LOGIC (SMART)
# ==============================
def entry_signal(df, direction):

    last = df.iloc[-1]
    prev = df.iloc[-2]

    swing_high = df['high'].tail(20).max()
    swing_low = df['low'].tail(20).min()

    # tolerance zone (improves fill rate)
    tolerance = 0.002

    # BUY
    if direction == "BUY":

        # breakout confirmation
        if last['close'] <= prev['high']:
            return None

        entry = last['ema50']

        if abs(last['close'] - entry) / entry > 0.02:
            return None

        sl = swing_low
        tp = swing_high

        risk = entry - sl
        reward = tp - entry

    # SELL
    elif direction == "SELL":

        if last['close'] >= prev['low']:
            return None

        entry = last['ema50']

        if abs(last['close'] - entry) / entry > 0.02:
            return None

        sl = swing_high
        tp = swing_low

        risk = sl - entry
        reward = entry - tp

    else:
        return None

    if risk <= 0 or reward <= 0:
        return None

    rr = round(reward / risk, 2)

    # avoid trash trades but not strict
    if rr < 1.2:
        return None

    return direction, entry, sl, tp, rr

# ==============================
# FINAL SIGNAL
# ==============================
def generate_filtered_signal(df_15m, df_1h, df_4h, df_1d):

    # skip bad market
    if not is_trending(df_4h):
        return None

    # check reversal first
    reversal = detect_htf_reversal(df_4h, df_1d)

    if reversal:
        return entry_signal(df_15m, reversal)

    # normal trend
    bias = get_htf_bias(df_1h, df_4h, df_1d)

    if not bias:
        return None

    return entry_signal(df_15m, bias)