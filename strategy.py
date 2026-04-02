import pandas as pd
import numpy as np


# ==============================
# INDICATORS
# ==============================
def apply_indicators(df):
    # Trend EMAs
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()

    # RSI
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    # Stochastic RSI (better overbought/oversold timing than plain RSI)
    rsi_min = df['rsi'].rolling(14).min()
    rsi_max = df['rsi'].rolling(14).max()
    stoch = (df['rsi'] - rsi_min) / (rsi_max - rsi_min + 1e-9)
    df['stoch_k'] = stoch.rolling(3).mean() * 100
    df['stoch_d'] = df['stoch_k'].rolling(3).mean()

    # MACD
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']

    # ATR (dynamic SL/TP sizing)
    hl = df['high'] - df['low']
    hc = (df['high'] - df['close'].shift()).abs()
    lc = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df['atr'] = tr.ewm(span=14, adjust=False).mean()

    # ADX (trend strength filter)
    plus_dm = df['high'].diff()
    minus_dm = -df['low'].diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    atr_s = tr.ewm(span=14, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(span=14, adjust=False).mean() / atr_s)
    minus_di = 100 * (minus_dm.ewm(span=14, adjust=False).mean() / atr_s)
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    df['adx'] = dx.ewm(span=14, adjust=False).mean()
    df['plus_di'] = plus_di
    df['minus_di'] = minus_di

    # Volume MA
    df['vol_ma'] = df['volume'].rolling(20).mean()

    return df


# ==============================
# MARKET CONDITION
# ==============================
def is_trending(df):
    """Require ADX > 22 — filters ranging/choppy markets."""
    last = df.iloc[-1]
    adx = last['adx']
    return not pd.isna(adx) and adx > 22


# ==============================
# STRUCTURE BIAS
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
# HTF BIAS (5-POINT CONFLUENCE)
# ==============================
def get_htf_bias(df_1h, df_4h, df_1d):
    """
    Score confluence across structure + EMA alignment.
    Require >= 4/5 points for a valid bias — reduces false signals.
    """
    last_1h = df_1h.iloc[-1]
    last_4h = df_4h.iloc[-1]

    bull_score = 0
    bear_score = 0

    # Structure (3 timeframes)
    for df in [df_1h, df_4h, df_1d]:
        bias = structure_bias(df)
        if bias == "bullish":
            bull_score += 1
        elif bias == "bearish":
            bear_score += 1

    # EMA 50/200 alignment on 1h
    if last_1h['ema50'] > last_1h['ema200']:
        bull_score += 1
    else:
        bear_score += 1

    # DI alignment on 4h
    if last_4h['plus_di'] > last_4h['minus_di']:
        bull_score += 1
    else:
        bear_score += 1

    if bull_score >= 4:
        return "BUY"
    if bear_score >= 4:
        return "SELL"
    return None


# ==============================
# HTF REVERSAL DETECTION
# ==============================
def detect_htf_reversal(df_4h, df_1d):
    """
    Requires: diverging structure + extreme StochRSI + volume surge + MACD flip.
    All 4 conditions needed — avoids premature reversal calls.
    """
    trend_4h = structure_bias(df_4h)
    trend_1d = structure_bias(df_1d)
    last = df_4h.iloc[-1]

    vol_surge = last['volume'] > last['vol_ma'] * 1.5

    # SELL reversal
    if trend_1d == "bullish" and trend_4h == "bearish":
        if last['stoch_k'] > 75 and vol_surge and last['macd_hist'] < 0:
            return "SELL"

    # BUY reversal
    if trend_1d == "bearish" and trend_4h == "bullish":
        if last['stoch_k'] < 25 and vol_surge and last['macd_hist'] > 0:
            return "BUY"

    return None


# ==============================
# ENGULFING PATTERN
# ==============================
def is_engulfing(df, direction):
    if len(df) < 2:
        return False
    last = df.iloc[-1]
    prev = df.iloc[-2]

    if direction == "BUY":
        return (
            prev['close'] < prev['open']       # prev bearish
            and last['close'] > last['open']    # current bullish
            and last['open'] <= prev['close']
            and last['close'] >= prev['open']
        )
    if direction == "SELL":
        return (
            prev['close'] > prev['open']
            and last['close'] < last['open']
            and last['open'] >= prev['close']
            and last['close'] <= prev['open']
        )
    return False


# ==============================
# TREND ENTRY SIGNAL
# ==============================
def entry_signal_trend(df_15m, df_1h, direction):
    last = df_15m.iloc[-1]
    last_1h = df_1h.iloc[-1]

    atr = last['atr']
    if pd.isna(atr) or atr <= 0:
        return None

    if direction == "BUY":
        if last['ema9'] < last['ema21']:        # LTF uptrend required
            return None
        if last['stoch_k'] > 72:                # Skip overbought entries
            return None
        if last_1h['macd_hist'] <= 0:           # 1h MACD must be bullish
            return None
        if last['volume'] < last['vol_ma'] * 1.15:  # Volume confirmation
            return None

        entry = last['close']
        sl = entry - (1.5 * atr)
        tp = entry + (3.5 * atr)                # ~2.33 RR

    elif direction == "SELL":
        if last['ema9'] > last['ema21']:
            return None
        if last['stoch_k'] < 28:
            return None
        if last_1h['macd_hist'] >= 0:
            return None
        if last['volume'] < last['vol_ma'] * 1.15:
            return None

        entry = last['close']
        sl = entry + (1.5 * atr)
        tp = entry - (3.5 * atr)

    else:
        return None

    risk = abs(entry - sl)
    reward = abs(tp - entry)

    if risk <= 0 or reward <= 0:
        return None

    rr = round(reward / risk, 2)
    if rr < 2.0:
        return None

    return direction, entry, sl, tp, rr, atr, "trend"


# ==============================
# REVERSAL ENTRY SIGNAL
# ==============================
def entry_signal_reversal(df_15m, df_1h, direction):
    """
    Reversal entries require engulfing pattern + extreme StochRSI + strong volume.
    Tighter SL multiplier (1.2x ATR) for better RR.
    """
    if not is_engulfing(df_15m, direction):
        return None

    last = df_15m.iloc[-1]

    atr = last['atr']
    if pd.isna(atr) or atr <= 0:
        return None

    if direction == "BUY":
        if last['stoch_k'] > 35:               # Must come from oversold
            return None
        if last['volume'] < last['vol_ma'] * 1.3:
            return None

        entry = last['close']
        sl = entry - (1.2 * atr)
        tp = entry + (3.0 * atr)               # 2.5 RR

    elif direction == "SELL":
        if last['stoch_k'] < 65:
            return None
        if last['volume'] < last['vol_ma'] * 1.3:
            return None

        entry = last['close']
        sl = entry + (1.2 * atr)
        tp = entry - (3.0 * atr)

    else:
        return None

    risk = abs(entry - sl)
    reward = abs(tp - entry)

    if risk <= 0 or reward <= 0:
        return None

    rr = round(reward / risk, 2)
    if rr < 2.0:
        return None

    return direction, entry, sl, tp, rr, atr, "reversal"


# ==============================
# FINAL SIGNAL
# ==============================
def generate_filtered_signal(df_15m, df_1h, df_4h, df_1d):
    # Hard gate: 4h must be trending (ADX > 22)
    if not is_trending(df_4h):
        return None

    # Reversal check first (higher RR potential)
    reversal = detect_htf_reversal(df_4h, df_1d)
    if reversal:
        result = entry_signal_reversal(df_15m, df_1h, reversal)
        if result:
            direction, entry, sl, tp, rr, atr, trade_type = result
            return direction, entry, sl, tp, rr, atr, trade_type

    # Trend following
    bias = get_htf_bias(df_1h, df_4h, df_1d)
    if not bias:
        return None

    result = entry_signal_trend(df_15m, df_1h, bias)
    if result:
        direction, entry, sl, tp, rr, atr, trade_type = result
        return direction, entry, sl, tp, rr, atr, trade_type

    return None
