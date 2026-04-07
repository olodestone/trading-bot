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
    df['vol_ma'] = df['volume'].rolling(20).median()

    return df


# ==============================
# MARKET CONDITION
# ==============================
def get_regime_params(df_4h, market_mode="normal"):
    """
    Detects market volatility regime from 4h ATR percentile rank.
    Returns adaptive thresholds so the strategy loosens in high-vol
    markets (more signals, bigger moves) and tightens in low-vol
    markets (only the cleanest setups).

    HIGH  (ATR rank > 70th pct): ADX 18, StochRSI 78/22, RR 2.0
    NORMAL(30–70th pct):         ADX 22, StochRSI 72/28, RR 2.5
    LOW   (ATR rank < 30th pct): ADX 25, StochRSI 68/32, RR 3.0

    Market mode overrides (applied on top of regime):
      bear     — ADX minimum reduced by 3 (ADX lags in early crash phases)
      recovery — RR minimum set to 2.0 (first-pullback longs have best edge)
    """
    atr = df_4h['atr'].dropna()
    if len(atr) < 50:
        params = {"adx_min": 22, "stoch_ob": 72, "stoch_os": 28, "rr_min": 2.5, "high_vol": False}
    else:
        rank = float((atr < atr.iloc[-1]).mean())   # 0.0 – 1.0

        if rank > 0.70:
            params = {"adx_min": 18, "stoch_ob": 78, "stoch_os": 22, "rr_min": 2.0, "high_vol": True}
        elif rank < 0.30:
            params = {"adx_min": 25, "stoch_ob": 68, "stoch_os": 32, "rr_min": 3.0, "high_vol": False}
        else:
            params = {"adx_min": 22, "stoch_ob": 72, "stoch_os": 28, "rr_min": 2.5, "high_vol": False}

    if market_mode == "bear":
        # ADX lags during early crash phases — trend is real even when ADX hasn't
        # had 14 bars to accumulate. Drop by 3 to catch sustained downtrends.
        params["adx_min"] = max(params["adx_min"] - 3, 14)
    elif market_mode == "recovery":
        # Best longs come on the first pullback after a bear phase ends.
        # Relax RR minimum so we don't miss the bulk of the up move.
        params["rr_min"] = 2.0

    return params


def is_trending(df, adx_min=22):
    """Require ADX > adx_min — filters ranging/choppy markets."""
    last = df.iloc[-1]
    adx = last['adx']
    return not pd.isna(adx) and adx > adx_min


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
def get_htf_bias(df_1h, df_4h, df_1d, params=None, market_mode="normal"):
    """
    Score confluence across structure + EMA alignment.
    Require >= 4/5 points for a valid bias — reduces false signals.

    Bear mode: SELL only, threshold reduced by 1. In a broad market crash,
    requiring full 4/5 bearish confluence blocks every short because one
    non-bearish element (e.g. 1d structure not yet broken) is always present.
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

    base_threshold = 3 if params and params.get("high_vol") else 4

    if market_mode == "bear":
        # SELL only — BUY signals are disabled at the caller level.
        # Reduce threshold by 1: crash markets always have one lagging
        # non-bearish factor (e.g. daily structure not yet confirmed).
        sell_threshold = base_threshold - 1
        if bear_score >= sell_threshold:
            return "SELL"
        return None

    if bull_score >= base_threshold:
        return "BUY"
    if bear_score >= base_threshold:
        return "SELL"
    return None


# ==============================
# HTF REVERSAL DETECTION
# ==============================
def detect_htf_reversal(df_4h, df_1d, params):
    """
    Requires: diverging structure + extreme StochRSI + volume surge + MACD flip.
    All 4 conditions needed — avoids premature reversal calls.
    StochRSI extremes adapt to the current volatility regime.
    """
    trend_4h = structure_bias(df_4h)
    trend_1d = structure_bias(df_1d)
    last = df_4h.iloc[-1]

    stoch_ob = params["stoch_ob"]
    stoch_os = params["stoch_os"]
    vol_surge = last['volume'] > last['vol_ma'] * 1.5

    # SELL reversal
    if trend_1d == "bullish" and trend_4h == "bearish":
        if last['stoch_k'] > stoch_ob and vol_surge and last['macd_hist'] < 0:
            return "SELL"

    # BUY reversal
    if trend_1d == "bearish" and trend_4h == "bullish":
        if last['stoch_k'] < stoch_os and vol_surge and last['macd_hist'] > 0:
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
# SUPPORT BOUNCE ENTRY
# ==============================
def entry_signal_bounce(df_15m, df_1h, df_4h, params):
    """
    Catches the FIRST touch of a major support level while the 4h is still
    structurally bearish — the setup the existing reversal system misses.

    The existing reversal requires trend_4h == "bullish" (structure already
    recovered). This fires before that, at the actual swing low.

    Gates (all required):
      1. 4h StochRSI < 20  — deeply oversold, hardcoded extreme (not regime-relative)
      2. 4h MACD histogram diverging up  — momentum turning even if price hasn't
      3. Price within 1.5% of a prior 1h swing low  — AT structural support
      4. 15m engulfing (bullish) OR hammer  — reversal candle confirming the bounce
      5. Volume ≥ 1.5× vol_ma (or ≥ 0.8× when stoch_k < 15 extreme oversold)
      6. RR ≥ params["rr_min"] + 0.5  — adaptive regime base + counter-trend premium:
           HIGH vol → 2.5, NORMAL → 3.0, LOW vol → 3.5
           Counter-trend always needs more reward than a trend entry in the same regime.
    """
    if len(df_15m) < 3 or len(df_4h) < 3:
        return None

    last_4h = df_4h.iloc[-1]
    prev_4h = df_4h.iloc[-2]
    last    = df_15m.iloc[-1]

    atr = last['atr']
    if pd.isna(atr) or atr <= 0:
        return None

    # Gate 1: 4h deeply oversold — structural extreme
    if last_4h['stoch_k'] > 20:
        return None

    # Plan B: extreme oversold flag — when stoch_k < 15, volume requirement
    # is relaxed from 1.5× to 0.8×. Crash conditions inflate vol_ma even with
    # median; the first recovery candle at structural support reads artificially
    # low against that inflated baseline. The stoch_k < 15 gate compensates.
    extreme_oversold = last_4h['stoch_k'] < 15

    # Gate 2: 4h MACD histogram turning up (diverging) — not yet flipped, just turning
    if last_4h['macd_hist'] <= prev_4h['macd_hist']:
        return None

    entry = last['close']

    # Gate 3: price must be sitting within 1.5% of a prior 1h swing low
    prior_lows = [l for l in swing_lows(df_1h) if abs(entry - l) / entry <= 0.015]
    if not prior_lows:
        return None

    # Gate 3b: extreme oversold only — before allowing the 0.8× volume relaxation,
    # confirm a momentum shift has begun. Standard oversold (15–20) keeps the 1.5×
    # volume gate which already enforces real buying; no extra gate needed there.
    # Prevents entering a continuation dump at the relaxed threshold.
    if extreme_oversold:
        nearest_low      = min(prior_lows, key=lambda l: abs(entry - l))
        stoch_rising     = last_4h['stoch_k'] > prev_4h['stoch_k']
        price_reclaiming = entry >= nearest_low   # close at or above support = holding
        if not (stoch_rising or price_reclaiming):
            return None

    # Gate 4: 15m bullish reaction candle — engulfing OR hammer
    body       = abs(last['close'] - last['open'])
    lower_wick = min(last['open'], last['close']) - last['low']
    upper_wick = last['high'] - max(last['open'], last['close'])
    is_hammer  = body > 0 and lower_wick >= 2 * body and upper_wick <= body
    if not is_engulfing(df_15m, "BUY") and not is_hammer:
        return None

    # Gate 5: volume check — threshold depends on how oversold 4h is.
    # Extreme oversold (stoch_k < 15): 0.8× — first recovery candle at a crash
    #   low is the highest-probability bounce but vol_ma is still inflated.
    # Standard oversold (15–20): 1.5× — require real buying absorption.
    vol_min = 0.8 if extreme_oversold else 1.5
    if last['volume'] < last['vol_ma'] * vol_min:
        return None

    # SL below recent 10-bar low — tight, behind the support zone
    sl   = df_15m['low'].tail(10).min() - (0.3 * atr)
    risk = entry - sl
    if risk <= 0:
        return None

    tp1 = nearest_resistance(df_1h, entry)
    if tp1 is None:
        return None
    reward = tp1 - entry
    if reward <= 0:
        return None

    rr = round(reward / risk, 2)
    rr_min = params["rr_min"] + 0.5   # regime base + counter-trend premium

    tp2 = second_resistance(df_1h, tp1)

    if rr >= rr_min:
        pass
    elif tp2 is not None:
        tp2_rr = round((tp2 - entry) / risk, 2)
        if tp2_rr < rr_min or rr < 1.5:
            return None
    else:
        return None

    return "BUY", entry, sl, tp1, tp2, rr, atr, "bounce"


# ==============================
# FADE RESISTANCE ENTRY (BEAR MODE SELL)
# ==============================
def entry_signal_fade_resistance(df_15m, df_4h, df_1h, params):
    """
    Plan A: Rally-to-resistance SELL in a confirmed 4h downtrend.

    Price bounces in a bear market and presses into the 4h EMA50 (dynamic
    resistance). A bearish reversal candle forms at that level, then the
    NEXT 15m candle closes below its low — confirming sellers took control.

    Intentionally bypasses 1h MACD bull: the bullish MACD is the symptom of
    the rally that delivered price to resistance, not a new uptrend. The 4h
    ema50 < ema200 structure is the authority.

    Gates (all required):
      1. 4h ema50 < ema200                   — bear structure confirmed
      2. Reversal candle high within 2% of 4h EMA50  — at dynamic resistance
      3. 4h stoch_k > 60                     — rally reached resistance (not OS)
      4. Prev 15m: shooting star OR bearish engulfing  — reversal candle at level
      5. Last 15m: close < prev['low']        — break-of-low confirmation
      6. Volume 0.8x–1.2x on confirmation    — normal fade vol; spike = breakout
    """
    if len(df_15m) < 3 or len(df_4h) < 3:
        return None

    last_4h  = df_4h.iloc[-1]
    last     = df_15m.iloc[-1]
    prev     = df_15m.iloc[-2]   # the reversal candle that formed at resistance
    prev_prev = df_15m.iloc[-3]  # candle before it (needed for engulfing check)

    atr = last['atr']
    if pd.isna(atr) or atr <= 0:
        return None

    # Gate 1: 4h bear structure confirmed
    if last_4h['ema50'] >= last_4h['ema200']:
        return None

    # Gate 2: reversal candle's high was within 2% of 4h EMA50 (tested resistance)
    ema50_4h = last_4h['ema50']
    price_ref = max(prev['high'], prev['close'])
    if abs(price_ref - ema50_4h) / ema50_4h > 0.02:
        return None

    # Gate 3: 4h stoch_k > 60 — rally has momentum, not already washed out
    if last_4h['stoch_k'] < 60:
        return None

    # Gate 4: prev 15m candle is a bearish reversal
    # Shooting star: body at bottom, upper wick ≥ 2× body, lower wick ≤ body
    prev_body        = abs(prev['close'] - prev['open'])
    prev_upper_wick  = prev['high'] - max(prev['open'], prev['close'])
    prev_lower_wick  = min(prev['open'], prev['close']) - prev['low']
    is_shooting_star = (
        prev_body > 0
        and prev_upper_wick >= 2 * prev_body
        and prev_lower_wick <= prev_body
    )
    # Bearish engulfing: prev_prev bullish, prev bearish and engulfs it
    is_bearish_engulf = (
        prev_prev['close'] > prev_prev['open']
        and prev['close'] < prev['open']
        and prev['open'] >= prev_prev['close']
        and prev['close'] <= prev_prev['open']
    )
    if not is_shooting_star and not is_bearish_engulf:
        return None

    # Gate 5: current candle closed below the reversal candle's low (confirmed break)
    if last['close'] >= prev['low']:
        return None

    # Gate 6: volume on confirmation candle in 0.8x–1.2x range
    # Below 0.8x: too quiet, move has no conviction
    # Above 1.2x: high volume at this level = buyers pushing through, not a fade
    vol_ratio = last['volume'] / last['vol_ma'] if last['vol_ma'] > 0 else 0
    if vol_ratio < 0.8 or vol_ratio > 1.2:
        return None

    # SL above the reversal candle's high — that's the invalidation point
    entry = last['close']
    sl    = prev['high'] + (0.3 * atr)
    risk  = sl - entry
    if risk <= 0:
        return None

    tp1 = nearest_support(df_1h, entry)
    if tp1 is None:
        return None
    reward = entry - tp1
    if reward <= 0:
        return None

    rr   = round(reward / risk, 2)
    rr_min = params["rr_min"]

    tp2 = second_support(df_1h, tp1)

    if rr >= rr_min:
        pass
    elif tp2 is not None:
        tp2_rr = round(abs(tp2 - entry) / risk, 2)
        if tp2_rr < rr_min or rr < 1.5:
            return None
    else:
        return None

    return "SELL", entry, sl, tp1, tp2, rr, atr, "fade"


# ==============================
# TREND ENTRY SIGNAL
# ==============================
def entry_signal_trend(df_15m, df_1h, direction, params, market_mode="normal"):
    if len(df_15m) < 3:
        return None

    last = df_15m.iloc[-1]
    prev = df_15m.iloc[-2]
    last_1h = df_1h.iloc[-1]

    stoch_ob = params["stoch_ob"]
    stoch_os = params["stoch_os"]
    rr_min   = params["rr_min"]

    atr = last['atr']
    if pd.isna(atr) or atr <= 0:
        return None

    if direction == "BUY":
        # Explosive breakout trigger: close must clear previous candle's high
        if last['close'] <= prev['high']:
            return None
        if last['stoch_k'] > stoch_ob:
            # OB on a strong breakout confirms momentum — don't block it.
            # Only reject when the breakout is weak or volume is absent.
            strong_breakout = (last['close'] - prev['high']) > 0.3 * atr
            strong_volume   = last['volume'] > last['vol_ma'] * 1.5
            if not (strong_breakout and strong_volume):
                return None
        if last_1h['macd_hist'] <= 0:               # 1h MACD must be bullish
            return None
        if last['volume'] < last['vol_ma'] * 1.15:  # Volume confirmation
            return None
        # Compression → expansion: coil before the breakout
        # In HIGH vol regime (crash/breakout already underway) the market won't
        # consolidate first — skip these gates so we don't miss the whole move.
        if not params.get("high_vol"):
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
        # StochRSI oversold check removed for trend shorts: in a sustained downtrend
        # the 15m stoch stays pinned near 0, which would permanently block SELL entries.
        # Oversold-zone filtering is correct for reversals but wrong for trend-following.
        if last_1h['macd_hist'] >= 0:
            return None

        # Bear mode: volume threshold relaxed to 0.90× — volume dries up
        # market-wide during panic phases; 0.9× with a clean break is meaningful.
        # Normal/recovery: require the usual 1.15× confirmation.
        vol_thresh = 0.90 if market_mode == "bear" else 1.15
        if last['volume'] < last['vol_ma'] * vol_thresh:
            return None

        # Bear mode: skip BB squeeze and coil — crashes move in steps, not from
        # tight coils. HIGH vol already bypasses these; extend to all regimes in bear.
        if market_mode != "bear" and not params.get("high_vol"):
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

    # Compute TP2 before the RR gate so it can rescue a marginal TP1.
    tp2 = second_resistance(df_1h, tp1) if direction == "BUY" else second_support(df_1h, tp1)

    if rr >= rr_min:
        pass  # TP1 alone is sufficient — allow regardless of TP2
    elif tp2 is not None:
        # TP1 is marginal — TP2 proves structure has room.
        # Require TP2 RR ≥ rr_min and TP1 RR ≥ 1.5 (floor — first partial must be meaningful).
        tp2_rr = round(abs(tp2 - entry) / risk, 2)
        if tp2_rr < rr_min or rr < 1.5:
            return None
    else:
        return None  # No TP2 to rescue marginal TP1

    return direction, entry, sl, tp1, tp2, rr, atr, "trend"


# ==============================
# REVERSAL ENTRY SIGNAL
# ==============================
def entry_signal_reversal(df_15m, df_1h, direction, params):
    """
    Reversal entries require engulfing pattern + extreme StochRSI + strong volume.
    SL is placed behind the engulfing candle itself — the candle defines the
    invalidation point. TP1 is the nearest structural level on 1h, TP2 is the next.
    StochRSI extremes and minimum RR adapt to the current volatility regime.
    """
    if not is_engulfing(df_15m, direction):
        return None

    last = df_15m.iloc[-1]

    stoch_ob = params["stoch_ob"]
    stoch_os = params["stoch_os"]
    rr_min   = params["rr_min"]

    atr = last['atr']
    if pd.isna(atr) or atr <= 0:
        return None

    if direction == "BUY":
        if last['stoch_k'] > stoch_os + 10:         # Must come from oversold zone
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
        if last['stoch_k'] < stoch_ob - 10:         # Must come from overbought zone
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

    tp2 = second_resistance(df_1h, tp1) if direction == "BUY" else second_support(df_1h, tp1)

    if rr >= rr_min:
        pass
    elif tp2 is not None:
        tp2_rr = round(abs(tp2 - entry) / risk, 2)
        if tp2_rr < rr_min or rr < 1.5:
            return None
    else:
        return None

    return direction, entry, sl, tp1, tp2, rr, atr, "reversal"


# ==============================
# FINAL SIGNAL
# ==============================
def generate_filtered_signal(df_15m, df_1h, df_4h, df_1d, symbol="", market_mode="normal"):
    # Detect regime once — all signal functions share these adaptive thresholds
    params = get_regime_params(df_4h, market_mode)

    # Use stoch_ob to infer the per-pair ATR regime — unaffected by bear mode's ADX adjustment
    regime = "HIGH" if params.get("high_vol") else ("LOW" if params["stoch_ob"] == 68 else "NORMAL")

    mode_tag = f"|{market_mode.upper()}" if market_mode != "normal" else ""
    regime_label = f"{regime}{mode_tag}"

    # Hard gate: 4h must be trending (adaptive ADX threshold, reduced in bear mode)
    if not is_trending(df_4h, params["adx_min"]):
        adx_val = df_4h.iloc[-1]['adx']
        print(f"  ↳ {symbol}: ADX {adx_val:.1f} < {params['adx_min']} [{regime_label}] — skip")
        return None

    # Priority routing — fade vs bounce ordering depends on proximity to resistance.
    # When price is within 1.5% of 4h EMA50 in bear mode, a bounce BUY would be
    # entering directly into overhead resistance. Fade SELL takes priority there.
    # When price is NOT near EMA50, bounce at support fires first as normal.
    near_ema50_resistance = False
    if market_mode == "bear":
        _ema50_4h   = df_4h.iloc[-1]['ema50']
        _last_close = df_15m.iloc[-1]['close']
        near_ema50_resistance = abs(_last_close - _ema50_4h) / _ema50_4h <= 0.015

    # Near EMA50 resistance: fade first, then bounce
    if near_ema50_resistance:
        fade = entry_signal_fade_resistance(df_15m, df_4h, df_1h, params)
        if fade:
            direction, entry, sl, tp1, tp2, rr, atr, trade_type = fade
            return direction, entry, sl, tp1, tp2, rr, atr, trade_type

    # Support bounce — fires at actual bottom before 4h structure turns.
    # Works in all modes. Near-resistance case: bounce still attempted if fade failed.
    bounce = entry_signal_bounce(df_15m, df_1h, df_4h, params)
    if bounce:
        direction, entry, sl, tp1, tp2, rr, atr, trade_type = bounce
        return direction, entry, sl, tp1, tp2, rr, atr, trade_type

    # Fade for bear mode when NOT near EMA50 resistance
    # (near-resistance case already checked above before bounce).
    if market_mode == "bear" and not near_ema50_resistance:
        fade = entry_signal_fade_resistance(df_15m, df_4h, df_1h, params)
        if fade:
            direction, entry, sl, tp1, tp2, rr, atr, trade_type = fade
            return direction, entry, sl, tp1, tp2, rr, atr, trade_type

    # Structure-confirmed reversal (higher RR potential, requires 1d/4h divergence).
    # Re-enabled in bear mode — the existing gates (4h structure bullish + vol surge +
    # MACD flip) are strict enough on their own; bear mode should not block this.
    reversal = detect_htf_reversal(df_4h, df_1d, params)
    if reversal:
        result = entry_signal_reversal(df_15m, df_1h, reversal, params)
        if result:
            direction, entry, sl, tp1, tp2, rr, atr, trade_type = result
            return direction, entry, sl, tp1, tp2, rr, atr, trade_type
        print(f"  ↳ {symbol}: reversal {reversal} detected but entry conditions not met")

    # Trend following
    bias = get_htf_bias(df_1h, df_4h, df_1d, params, market_mode)
    if not bias:
        last_1h = df_1h.iloc[-1]
        last_4h = df_4h.iloc[-1]
        # In bear mode the threshold is reduced by 1 for SELL — show correct number
        base_threshold = 3 if params.get("high_vol") else 4
        threshold = base_threshold - 1 if market_mode == "bear" else base_threshold
        print(f"  ↳ {symbol}: HTF bias < {threshold}/5 [{regime_label}] ema50={'>' if last_1h['ema50'] > last_1h['ema200'] else '<'}ema200 di+={'>' if last_4h['plus_di'] > last_4h['minus_di'] else '<'}di-")
        return None

    result = entry_signal_trend(df_15m, df_1h, bias, params, market_mode)
    if result:
        direction, entry, sl, tp1, tp2, rr, atr, trade_type = result
        return direction, entry, sl, tp1, tp2, rr, atr, trade_type

    # Log why trend entry was rejected
    last = df_15m.iloc[-1]
    prev = df_15m.iloc[-2]
    last_1h = df_1h.iloc[-1]
    atr = last['atr']
    reasons = []
    if bias == "BUY":
        if last['close'] <= prev['high']:
            reasons.append("no breakout")
        if last['stoch_k'] > params['stoch_ob']:
            strong_breakout = (last['close'] - prev['high']) > 0.3 * atr
            strong_volume   = last['volume'] > last['vol_ma'] * 1.5
            if not (strong_breakout and strong_volume):
                reasons.append(f"stoch OB {last['stoch_k']:.0f}>{params['stoch_ob']} weak-breakout")
        if last_1h['macd_hist'] <= 0:
            reasons.append("1h MACD bear")
        if last['volume'] < last['vol_ma'] * 1.15:
            reasons.append(f"vol low {last['volume']/last['vol_ma']:.2f}x")
        if not params.get("high_vol") and not is_bb_squeeze(df_15m):
            reasons.append("no BB squeeze")
        if not params.get("high_vol") and not consolidation_coil(df_15m, atr):
            reasons.append("no coil")
    else:
        if last['close'] >= prev['low']:
            reasons.append("no breakdown")
        if last_1h['macd_hist'] >= 0:
            reasons.append("1h MACD bull")
        # Bear mode uses 0.90× vol threshold; normal uses 1.15×
        vol_thresh = 0.90 if market_mode == "bear" else 1.15
        if last['volume'] < last['vol_ma'] * vol_thresh:
            reasons.append(f"vol low {last['volume']/last['vol_ma']:.2f}x")
        # Bear mode skips BB/coil for SELL — only log these in normal/recovery
        if market_mode != "bear" and not params.get("high_vol") and not is_bb_squeeze(df_15m):
            reasons.append("no BB squeeze")
        if market_mode != "bear" and not params.get("high_vol") and not consolidation_coil(df_15m, atr):
            reasons.append("no coil")
    print(f"  ↳ {symbol}: {bias} entry rejected [{regime_label}] — {', '.join(reasons) if reasons else 'RR/TP failed'}")

    return None
