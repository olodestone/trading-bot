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

    # 🔥 NEW (reversal strength)
    df['vol_ma'] = df['volume'].rolling(20).mean()

    return df

# ==============================
# MARKET CONDITION
# ==============================
def is_trending(df):
    recent = df.tail(30)
    range_size = (recent['high'].max() - recent['low'].min()) / recent['close'].iloc[-1]
    return range_size > 0.015

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
# HTF REVERSAL DETECTION (UPGRADED)
# ==============================
def detect_htf_reversal(df_4h, df_1d):

    trend_4h = structure_bias(df_4h)
    trend_1d = structure_bias(df_1d)

    last = df_4h.iloc[-1]

    # SELL reversal
    if trend_1d == "bullish" and trend_4h == "bearish":
        if last['rsi'] > 70 and last['volume'] > last['vol_ma']:
            return "SELL"

    # BUY reversal
    if trend_1d == "bearish" and trend_4h == "bullish":
        if last['rsi'] < 30 and last['volume'] > last['vol_ma']:
            return "BUY"

    return None

# ==============================
# ENTRY LOGIC (UNCHANGED CORE)
# ==============================
def entry_signal(df, direction):

    last = df.iloc[-1]
    prev = df.iloc[-2]

    swing_high = df['high'].tail(30).max()
    swing_low = df['low'].tail(30).min()

    if direction == "BUY":

        if last['close'] <= prev['high']:
            return None

        entry = last['ema50']
        sl = swing_low
        tp = swing_high

        risk = entry - sl
        reward = tp - entry

    elif direction == "SELL":

        if last['close'] >= prev['low']:
            return None

        entry = last['ema50']
        sl = swing_high
        tp = swing_low

        risk = sl - entry
        reward = entry - tp

    else:
        return None

    if risk <= 0 or reward <= 0:
        return None

    if abs(entry - sl) / entry < 0.003:
        return None

    if abs(tp - entry) / entry < 0.004:
        return None

    rr = round(reward / risk, 2)

    if rr < 1.5:
        return None

    return direction, entry, sl, tp, rr, "trend"

# ==============================
# FINAL SIGNAL (UPDATED)
# ==============================
def generate_filtered_signal(df_15m, df_1h, df_4h, df_1d):

    if not is_trending(df_4h):
        return None

    reversal = detect_htf_reversal(df_4h, df_1d)

    if reversal:
        result = entry_signal(df_15m, reversal)
        if result:
            direction, entry, sl, tp, rr, _ = result
            return direction, entry, sl, tp, rr, "reversal"

    bias = get_htf_bias(df_1h, df_4h, df_1d)

    if not bias:
        return None

    result = entry_signal(df_15m, bias)
    if result:
        direction, entry, sl, tp, rr, _ = result
        return direction, entry, sl, tp, rr, "trend"