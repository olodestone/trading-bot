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
# BB SQUEEZE DETECTION
# ==============================
def is_bb_squeeze(df, window=20):
    """
    True when at least 4 of the last 5 candles had BBW below 85% of its
    50-candle average (compression), AND the current candle shows BBW
    expanding — the classic coil → breakout transition.
    """
    close = df['close']
    mid = close.rolling(window).mean()
    std = close.rolling(window).std()
    bbw = (4 * std) / mid.replace(0, np.nan)

    if bbw.dropna().shape[0] < 55:
        return False

    bbw_avg50 = bbw.rolling(50).mean()
    compressed = (bbw.iloc[-6:-1] < bbw_avg50.iloc[-6:-1] * 0.85).sum() >= 4
    expanding = bbw.iloc[-1] > bbw.iloc[-2]
    return bool(compressed and expanding)


# ==============================
# CONSOLIDATION COIL
# ==============================
def consolidation_coil(df, atr, min_candles=4):
    """
    True if min_candles consecutive candles before the last had a
    candle range < 0.6 × ATR — confirms tight coil before the move.
    """
    count = 0
    for i in range(2, min(len(df), 12)):
        candle = df.iloc[-i]
        if (candle['high'] - candle['low']) < 0.6 * atr:
            count += 1
        else:
            break
    return count >= min_candles


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
# STRUCTURE LEVELS
# ==============================
def swing_highs(df, order=2):
    """Candles where high is greater than `order` neighbours on each side."""
    highs = df['high'].values
    result = []
    for i in range(order, len(highs) - order):
        if all(highs[i] > highs[i - j] for j in range(1, order + 1)) and \
           all(highs[i] > highs[i + j] for j in range(1, order + 1)):
            result.append(highs[i])
    return result


def swing_lows(df, order=2):
    """Candles where low is less than `order` neighbours on each side."""
    lows = df['low'].values
    result = []
    for i in range(order, len(lows) - order):
        if all(lows[i] < lows[i - j] for j in range(1, order + 1)) and \
           all(lows[i] < lows[i + j] for j in range(1, order + 1)):
            result.append(lows[i])
    return result


def nearest_resistance(df_1h, entry):
    """Nearest swing high above entry on the 1h chart — TP1 target."""
    levels = [h for h in swing_highs(df_1h) if h > entry * 1.001]
    return min(levels) if levels else None


def nearest_support(df_1h, entry):
    """Nearest swing low below entry on the 1h chart — TP1 target."""
    levels = [l for l in swing_lows(df_1h) if l < entry * 0.999]
    return max(levels) if levels else None


def second_resistance(df_1h, tp1):
    """Next swing high above TP1 — TP2 target for trailing the runner."""
    levels = [h for h in swing_highs(df_1h) if h > tp1 * 1.001]
    return min(levels) if levels else None


def second_support(df_1h, tp1):
    """Next swing low below TP1 — TP2 target for trailing the runner."""
    levels = [l for l in swing_lows(df_1h) if l < tp1 * 0.999]
    return max(levels) if levels else None


# ==============================
# TREND ENTRY SIGNAL
# ==============================
def entry_signal_trend(df_15m, df_1h, direction):
    if len(df_15m) < 3:
        return None

    last = df_15m.iloc[-1]
    prev = df_15m.iloc[-2]
    last_1h = df_1h.iloc[-1]

    atr = last['atr']
    if pd.isna(atr) or atr <= 0:
        return None

    if direction == "BUY":
        # Explosive breakout trigger: close must clear previous candle's high
        if last['close'] <= prev['high']:
            return None
        if last['stoch_k'] > 72:                    # Skip overbought entries
            return None
        if last_1h['macd_hist'] <= 0:               # 1h MACD must be bullish
            return None
        if last['volume'] < last['vol_ma'] * 1.15:  # Volume confirmation
            return None
        # Compression → expansion: coil before the breakout
        if not is_bb_squeeze(df_15m):
            return None
        if not consolidation_coil(df_15m, atr):
            return None

        entry = last['close']
        sl = df_15m['low'].tail(20).min() - (0.3 * atr)
        risk = entry - sl
        if risk <= 0:
            return None

        tp1 = nearest_resistance(df_1h, entry)
        if tp1 is None:
            return None
        reward = tp1 - entry

    elif direction == "SELL":
        # Explosive breakdown trigger: close must break below previous candle's low
        if last['close'] >= prev['low']:
            return None
        if last['stoch_k'] < 28:
            return None
        if last_1h['macd_hist'] >= 0:
            return None
        if last['volume'] < last['vol_ma'] * 1.15:
            return None
        if not is_bb_squeeze(df_15m):
            return None
        if not consolidation_coil(df_15m, atr):
            return None

        entry = last['close']
        sl = df_15m['high'].tail(20).max() + (0.3 * atr)
        risk = sl - entry
        if risk <= 0:
            return None

        tp1 = nearest_support(df_1h, entry)
        if tp1 is None:
            return None
        reward = entry - tp1

    else:
        return None

    if reward <= 0:
        return None

    rr = round(reward / risk, 2)
    if rr < 2.5:
        return None

    # TP2: next structural level beyond TP1 — runner target
    tp2 = second_resistance(df_1h, tp1) if direction == "BUY" else second_support(df_1h, tp1)

    return direction, entry, sl, tp1, tp2, rr, atr, "trend"


# ==============================
# REVERSAL ENTRY SIGNAL
# ==============================
def entry_signal_reversal(df_15m, df_1h, direction):
    """
    Reversal entries require engulfing pattern + extreme StochRSI + strong volume.
    SL is placed behind the engulfing candle itself — the candle defines the
    invalidation point. TP1 is the nearest structural level on 1h, TP2 is the next.
    """
    if not is_engulfing(df_15m, direction):
        return None

    last = df_15m.iloc[-1]

    atr = last['atr']
    if pd.isna(atr) or atr <= 0:
        return None

    if direction == "BUY":
        if last['stoch_k'] > 35:                    # Must come from oversold
            return None
        if last['volume'] < last['vol_ma'] * 1.3:
            return None

        entry = last['close']
        sl = last['low'] - (0.3 * atr)
        risk = entry - sl
        if risk <= 0:
            return None

        tp1 = nearest_resistance(df_1h, entry)
        if tp1 is None:
            return None
        reward = tp1 - entry

    elif direction == "SELL":
        if last['stoch_k'] < 65:
            return None
        if last['volume'] < last['vol_ma'] * 1.3:
            return None

        entry = last['close']
        sl = last['high'] + (0.3 * atr)
        risk = sl - entry
        if risk <= 0:
            return None

        tp1 = nearest_support(df_1h, entry)
        if tp1 is None:
            return None
        reward = entry - tp1

    else:
        return None

    if reward <= 0:
        return None

    rr = round(reward / risk, 2)
    if rr < 2.5:
        return None

    tp2 = second_resistance(df_1h, tp1) if direction == "BUY" else second_support(df_1h, tp1)

    return direction, entry, sl, tp1, tp2, rr, atr, "reversal"


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
            direction, entry, sl, tp1, tp2, rr, atr, trade_type = result
            return direction, entry, sl, tp1, tp2, rr, atr, trade_type

    # Trend following
    bias = get_htf_bias(df_1h, df_4h, df_1d)
    if not bias:
        return None

    result = entry_signal_trend(df_15m, df_1h, bias)
    if result:
        direction, entry, sl, tp1, tp2, rr, atr, trade_type = result
        return direction, entry, sl, tp1, tp2, rr, atr, trade_type

    return None
