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
def generate_signal(df):
    last = df.iloc[-1]
    entry = last['close']

    swing_high, swing_low = get_structure_levels(df)

    bullish = last['close'] > last['ema50'] and last['rsi'] > 55
    bearish = last['close'] < last['ema50'] and last['rsi'] < 45

    # =======================
    # BUY SETUP
    # =======================
    if bullish:
        sl = swing_low          # 🔥 structure-based SL
        tp = swing_high         # 🔥 natural resistance

        risk = entry - sl
        reward = tp - entry

        if risk <= 0 or reward <= 0:
            return None

        rr = reward / risk
        signal = "BUY"

    # ==========================
    # SELL SETUP
    # ==========================
    elif bearish:
        sl = swing_high
        tp = swing_low

        risk = sl - entry
        reward = entry - tp

        if risk <= 0 or reward <= 0:
            return None

        rr = reward / risk
        signal = "SELL"

    else:
        return None

    rr = round(rr, 2)

    # 🔥 ONLY TAKE HIGH-QUALITY TRADES
    if rr < 3:
        return None

    return signal, entry, sl, tp, rr