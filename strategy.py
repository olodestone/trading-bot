import pandas as pd
import numpy as np
from datetime import datetime as _dt


# ==============================
# INDICATORS
# ==============================
def apply_indicators(df):
    # Trend EMAs
    df['ema9']   = df['close'].ewm(span=9,   adjust=False).mean()
    df['ema20']  = df['close'].ewm(span=20,  adjust=False).mean()
    df['ema21']  = df['close'].ewm(span=21,  adjust=False).mean()
    df['ema50']  = df['close'].ewm(span=50,  adjust=False).mean()
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

    Two separate ADX thresholds are produced:

      adx_route — routing decision (trending vs ranging path).
                  Lower bar: a pair at ADX 17 isn't trending enough for a
                  trend entry but still has directional bias — bounce/fade/
                  reversal should get a chance before falling back to range mode.

      adx_min   — entry quality gate inside trend entries.
                  Higher bar: the trend must be established before we commit
                  to a directional position. Used by is_trending() inside the
                  trend entry signal.

    Regime table (adx_route / adx_min):
      HIGH  vol (ATR > 70th pct): 15 / 19, StochRSI 78/22, RR 2.0
      NORMAL    (30–70th pct):    18 / 22, StochRSI 72/28, RR 2.5
      LOW   vol (ATR < 30th pct): 20 / 23, StochRSI 68/32, RR 3.0

    Bear mode reductions (per-regime — HIGH vol crashes spike ADX faster):
      HIGH vol bear:   adx_min 19→16 (−3),  adx_route 15→13 (−2)
      NORMAL vol bear: adx_min 22→20 (−2),  adx_route 18→16 (−2)
      LOW vol bear:    adx_min 23→21 (−2),  adx_route 20→18 (−2)

    Recovery mode: RR minimum → 2.0 (first-pullback longs have best edge)
    """
    atr = df_4h['atr'].dropna()
    if len(atr) < 50:
        # Fallback when not enough history — NORMAL defaults
        params = {"adx_min": 22, "adx_route": 18, "stoch_ob": 72, "stoch_os": 28, "rr_min": 2.5, "high_vol": False}
        _bear_min_adj, _bear_route_adj = 2, 2
    else:
        rank = float((atr < atr.iloc[-1]).mean())   # 0.0 – 1.0

        if rank > 0.70:
            # HIGH vol: explosive moves, ADX builds fast — lower entry bar
            params = {"adx_min": 19, "adx_route": 15, "stoch_ob": 78, "stoch_os": 22, "rr_min": 2.0, "high_vol": True}
            _bear_min_adj, _bear_route_adj = 3, 2   # 19→16, 15→13
        elif rank < 0.30:
            # LOW vol: slow-moving market, trends need longer to confirm — raise bar
            # adx_min 23 (not 25) — 25 is rarely achieved in genuinely quiet markets
            params = {"adx_min": 23, "adx_route": 20, "stoch_ob": 68, "stoch_os": 32, "rr_min": 3.0, "high_vol": False}
            _bear_min_adj, _bear_route_adj = 2, 2   # 23→21, 20→18
        else:
            # NORMAL vol
            params = {"adx_min": 22, "adx_route": 18, "stoch_ob": 72, "stoch_os": 28, "rr_min": 2.5, "high_vol": False}
            _bear_min_adj, _bear_route_adj = 2, 2   # 22→20, 18→16

    if market_mode == "bear":
        # ADX lags during early crash phases — trend is real before ADX confirms.
        # Reduction is per-regime: HIGH vol crashes spike ADX faster (needs -3),
        # NORMAL/LOW are slower-moving (needs only -2).
        params["adx_min"]   = max(params["adx_min"]   - _bear_min_adj,   14)
        params["adx_route"] = max(params["adx_route"] - _bear_route_adj, 10)
    elif market_mode == "recovery":
        # Best longs come on the first pullback after a bear phase ends.
        # Relax RR minimum so we don't miss the bulk of the up move.
        params["rr_min"] = 2.0

    # Store current 4h ADX so entry functions can check trend strength without
    # receiving df_4h directly — used for the BB/coil bypass (ADX > 28).
    params["adx_4h"] = float(df_4h.iloc[-1]["adx"]) if not pd.isna(df_4h.iloc[-1]["adx"]) else 0.0
    params["market_mode"] = market_mode

    return params


def is_trending(df, adx_min=22):
    """
    Require ADX > adx_min — filters ranging/choppy markets.

    Rising ADX bypass: if ADX is within 5 points of the threshold and has
    been rising for 3 bars, the trend is building. Allow it rather than
    waiting for ADX to cross after the move is already underway.
    A rising ADX at 18 is better than a falling ADX at 25.
    """
    last = df.iloc[-1]
    adx = last['adx']
    if pd.isna(adx):
        return False
    if adx > adx_min:
        return True
    # Rising bypass: ADX close to threshold and pointing up
    if len(df) >= 4 and adx > adx_min - 5:
        if df['adx'].iloc[-1] > df['adx'].iloc[-4]:
            return True
    return False


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
    4h DI direction is mandatory — never fire against the confirmed 4h trend.
    Score the remaining 4 factors: 1h/4h/1d structure + 1h EMA50/200 alignment.

    BUY threshold: 3/4 NORMAL vol, 2/4 HIGH vol.
    SELL threshold: 4/4 NORMAL vol, 3/4 HIGH vol (one step harder — mixed/bullish regimes produce false sells).

    Bear mode: SELL only. Threshold drops by 1 from the SELL baseline.
    """
    last_1h = df_1h.iloc[-1]
    last_4h = df_4h.iloc[-1]

    # Mandatory gate: 4h DI must confirm the direction.
    # A setup that scores well on structure but has DI pointing the wrong way
    # is fighting the strongest real-time trend signal we have.
    di_bull = last_4h['plus_di'] > last_4h['minus_di']
    di_bear = last_4h['minus_di'] > last_4h['plus_di']

    # Score the remaining 4 factors
    bull_score = 0
    bear_score = 0

    for df in [df_1h, df_4h, df_1d]:
        bias = structure_bias(df)
        if bias == "bullish":
            bull_score += 1
        elif bias == "bearish":
            bear_score += 1

    # 1h EMA50/200 alignment
    if last_1h['ema50'] > last_1h['ema200']:
        bull_score += 1
    else:
        bear_score += 1

    buy_threshold  = 2 if params and params.get("high_vol") else 3
    sell_threshold = min(buy_threshold + 1, 4)  # SELL is one step harder than BUY

    if market_mode == "bear":
        # SELL only — BUY disabled at caller level.
        # Threshold drops by 1: 1d structure lags in early crash phases.
        bear_sell_threshold = max(sell_threshold - 1, 1)
        if di_bear and bear_score >= bear_sell_threshold:
            return "SELL"
        return None

    if di_bull and bull_score >= buy_threshold:
        return "BUY"
    if di_bear and bear_score >= sell_threshold:
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
# SUPPORT / RESISTANCE BOUNCE ENTRY
# ==============================
def entry_signal_bounce(df_15m, df_1h, df_4h, params):
    """
    True fallback bounce — fires at any structural support OR resistance level.

    MANDATORY: price within ATR×0.5 of a prior swing low (BUY) or swing high (SELL).

    ONE confirmation required (not all):
      a) Reversal candle  — hammer/bullish-engulfing (BUY) or shooting-star/bearish-engulfing (SELL)
      b) RSI oversold/OB  — 4h stoch_k < 30 (BUY) or > 70 (SELL)
      c) MACD turning     — 4h macd_hist improving in signal direction

    SL: beyond the structural level (level ± 0.3 ATR).
    RR: ≥ params["rr_min"] (counter-trend premium dropped — one confirmation is enough gate).
    """
    if len(df_15m) < 3 or len(df_4h) < 3 or len(df_1h) < 3:
        return None

    last_4h = df_4h.iloc[-1]
    prev_4h = df_4h.iloc[-2]
    last    = df_15m.iloc[-1]
    prev    = df_15m.iloc[-2]

    atr = last['atr']
    if pd.isna(atr) or atr <= 0:
        return None

    entry = last['close']

    # ── Structural proximity (ATR-based tolerance) ─────────────────────────
    lows_1h  = swing_lows(df_1h)
    highs_1h = swing_highs(df_1h)
    lows_4h  = swing_lows(df_4h)
    highs_4h = swing_highs(df_4h)

    tol = atr * 0.5

    near_sup = [l for l in lows_1h + lows_4h  if abs(entry - l) <= tol and l < entry]
    near_res = [h for h in highs_1h + highs_4h if abs(entry - h) <= tol and h >= entry]

    # ── Confirmations ─────────────────────────────────────────────────────
    body_last       = abs(last['close'] - last['open'])
    lower_wick_last = min(last['open'], last['close']) - last['low']
    upper_wick_last = last['high'] - max(last['open'], last['close'])
    is_hammer       = body_last > 0 and lower_wick_last >= 2 * body_last and upper_wick_last <= body_last

    body_prev       = abs(prev['close'] - prev['open'])
    upper_wick_prev = prev['high'] - max(prev['open'], prev['close'])
    lower_wick_prev = min(prev['open'], prev['close']) - prev['low']
    is_shooting_star = body_prev > 0 and upper_wick_prev >= 2 * body_prev and lower_wick_prev <= body_prev

    stoch_k = last_4h.get('stoch_k', 50)
    macd_turning_up   = last_4h['macd_hist'] > prev_4h['macd_hist']
    macd_turning_down = last_4h['macd_hist'] < prev_4h['macd_hist']

    # ── BUY at support ─────────────────────────────────────────────────────
    if near_sup:
        nearest_sup = max(near_sup)
        dist_atr    = round((entry - nearest_sup) / atr, 2)

        # In recovery mode require structure to be turning bullish before buying.
        if params.get("market_mode") == "recovery":
            close_above_ema50 = entry > df_1h.iloc[-1].get("ema50", entry - 1)
            _sw_lows  = swing_lows(df_1h)
            higher_low = len(_sw_lows) >= 2 and _sw_lows[-1] > _sw_lows[-2]
            gate_pass  = close_above_ema50 and higher_low
            print(f"    bounce BUY: sup={nearest_sup:.4f} dist={dist_atr}ATR | recovery gate: ema50={'✓' if close_above_ema50 else '✗'} AND higher_low={'✓' if higher_low else '✗'} → {'pass' if gate_pass else 'BLOCK'}")
            if not gate_pass:
                return None

        # Hard gate: 4h stoch overbought means price is NOT at support — it's extended.
        # A bounce BUY at stoch_k > 70 is chasing, not buying a dip.
        if stoch_k > 70:
            print(f"    bounce BUY: stoch_k={stoch_k:.0f} > 70 — 4h overbought, not at support")
            return None

        conf_candle = is_engulfing(df_15m, "BUY") or is_hammer
        conf_rsi    = stoch_k < 30
        conf_macd   = macd_turning_up
        # Recovery mode: candle confirmation mandatory — MACD/RSI alone just means
        # "less bad", not a real reversal. Price must show actual rejection at support.
        if params.get("market_mode") == "recovery" and not conf_candle:
            print(f"    bounce BUY: recovery — candle required (stoch={stoch_k:.0f} macd={'↑' if conf_macd else '↓'})")
            return None
        if conf_candle or conf_rsi or conf_macd:
            sl_buf = 0.5 * atr if params.get("market_mode") == "recovery" else 0.3 * atr
            sl   = nearest_sup - sl_buf
            risk = entry - sl
            if risk < atr * 0.5:
                print(f"    bounce BUY: sup={nearest_sup:.4f} SL too tight (risk={risk:.4f} < 0.5×ATR={atr*0.5:.4f} buf={'0.5' if params.get('market_mode') == 'recovery' else '0.3'}×ATR)")
                return None
            if risk > 0 and last['volume'] >= last['vol_ma'] * 0.8:
                tp1 = nearest_resistance(df_1h, entry) or nearest_resistance(df_4h, entry)
                if tp1 is not None and tp1 > entry:
                    reward = tp1 - entry
                    rr = round(reward / risk, 2)
                    if rr >= params["rr_min"] and rr <= 6.5:
                        tp2 = second_resistance(df_1h, tp1)
                        confs = [c for c in ["candle", "rsi_os", "macd_turn"] if [conf_candle, conf_rsi, conf_macd][["candle", "rsi_os", "macd_turn"].index(c)]]
                        print(f"    bounce BUY conf={'+'.join(confs)} stoch={stoch_k:.0f} rr={rr}")
                        return "BUY", entry, sl, tp1, tp2, rr, atr, "bounce"
        else:
            print(f"    bounce BUY: sup={nearest_sup:.4f} dist={dist_atr}ATR | no confirmation (candle=✗ stoch={stoch_k:.0f} macd={'↑' if macd_turning_up else '↓'})")
    else:
        if params.get("market_mode") != "recovery":
            print(f"    bounce BUY: no structure near price (tol={round(tol, 4)})")

    # ── SELL at resistance ─────────────────────────────────────────────────
    # Skip SELL bounces in recovery mode — market is turning bullish; a SELL
    # queued here would lock out the directionally-correct BUY for 15+ minutes.
    if near_res and params.get("market_mode") != "recovery":
        nearest_res = min(near_res)
        dist_atr    = round((nearest_res - entry) / atr, 2)
        conf_candle = is_engulfing(df_15m, "SELL") or is_shooting_star
        conf_rsi    = stoch_k > 70
        conf_macd   = macd_turning_down
        # MACD turn alone is too weak — require candle or RSI OB, or MACD only when approaching OB
        strong_conf = conf_candle or conf_rsi or (conf_macd and stoch_k > 55)
        if strong_conf:
            sl   = nearest_res + 0.3 * atr
            risk = sl - entry
            # Minimum SL distance: 0.5×ATR — resistance at entry creates a trivially tight stop
            if risk < atr * 0.5:
                print(f"    bounce SELL: res={nearest_res:.4f} SL too tight (risk={risk:.4f} < 0.5×ATR={atr*0.5:.4f})")
                return None
            if risk > 0 and last['volume'] >= last['vol_ma'] * 0.8:
                tp1 = nearest_support(df_1h, entry) or nearest_support(df_4h, entry)
                if tp1 is not None and tp1 < entry:
                    reward = entry - tp1
                    rr = round(reward / risk, 2)
                    if rr >= params["rr_min"] and rr <= 6.5:
                        tp2 = second_support(df_1h, tp1)
                        confs = [c for c in ["candle", "rsi_ob", "macd_turn"] if [conf_candle, conf_rsi, conf_macd][["candle", "rsi_ob", "macd_turn"].index(c)]]
                        print(f"    bounce SELL conf={'+'.join(confs)} stoch={stoch_k:.0f} rr={rr}")
                        return "SELL", entry, sl, tp1, tp2, rr, atr, "bounce"
        else:
            print(f"    bounce SELL: res={nearest_res:.4f} dist={dist_atr}ATR | no confirmation (candle=✗ stoch={stoch_k:.0f} macd={'↓' if macd_turning_down else '↑'})")
    elif not near_res and params.get("market_mode") != "recovery":
        print(f"    bounce SELL: no structure near price (tol={round(tol, 4)})")

    return None


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
# MICRO TREND CONTINUATION
# ==============================
def entry_signal_micro_trend(df_15m, df_1h, params, market_mode="normal"):
    """
    Light momentum entry — catches trending pairs that never pull back deep enough
    for the pullback system.

    Gates:
      1. Price above EMA20 (BUY) / below EMA20 (SELL) — on the right side of momentum
      2. Small pullback recently — at least 1 of the last 3 candles touched within
         0.3×ATR of EMA20 (price paused, didn't reverse)
      3. Volume spike — current candle volume > vol_ma × 1.5 (real participation)
      4. 1H RSI not overbought/oversold — between 40 and 70 (BUY), 30 and 60 (SELL)
         (avoids entering at exhaustion)

    Exits:
      TP1: entry ± 1.0×ATR  — tight, realistic for momentum continuation
      SL:  EMA20 ± 0.3×ATR  — price back inside EMA20 = momentum failed
      RR:  must clear 1.2 minimum
    """
    if len(df_15m) < 5 or len(df_1h) < 1:
        return None

    last   = df_15m.iloc[-1]
    recent = df_15m.iloc[-4:-1]   # 3 candles before current

    atr   = last.get("atr", 0)
    ema20 = last.get("ema20")
    close = last["close"]

    if pd.isna(atr) or atr <= 0 or pd.isna(ema20):
        return None

    rsi_1h = df_1h.iloc[-1].get("rsi")
    if pd.isna(rsi_1h):
        return None

    # Volume spike
    if last["volume"] < last["vol_ma"] * 1.5:
        return None

    # Recovery mode blocks SELL, bear mode blocks BUY
    for direction in (["SELL"] if market_mode == "bear" else
                      ["BUY"]  if market_mode == "recovery" else
                      ["BUY", "SELL"]):

        if direction == "BUY":
            price_side    = close > ema20
            rsi_ok        = 40 <= rsi_1h <= 70
            pullback_near = any(abs(c["low"] - ema20) <= 0.3 * atr for _, c in recent.iterrows())
            sl            = ema20 - 0.3 * atr
        else:
            price_side    = close < ema20
            rsi_ok        = 30 <= rsi_1h <= 60
            pullback_near = any(abs(c["high"] - ema20) <= 0.3 * atr for _, c in recent.iterrows())
            sl            = ema20 + 0.3 * atr

        if not (price_side and rsi_ok and pullback_near):
            continue

        risk = abs(close - sl)
        if risk <= 0 or risk > close * 0.04:   # SL max 4% — micro entries must be tight
            continue
        if risk < atr * 0.5:                   # SL min 0.5×ATR — sub-noise stops are unworkable
            continue

        tp1 = close + atr if direction == "BUY" else close - atr
        rr  = round(abs(tp1 - close) / risk, 2)
        if rr < 1.2:
            continue

        return (direction, close, sl, tp1, None, rr, atr, "micro")

    return None


# ==============================
# RANGE ENTRY SIGNAL
# ==============================
def entry_signal_range(df_15m, df_1h, df_4h, params, market_mode="normal"):
    """
    Support/resistance range trade — fires when the pair is ranging (ADX below
    threshold). Instead of sitting idle, buy at structural support and sell at
    structural resistance. The range defines entry, SL, and target.

    BUY at support (disabled in bear mode — bounce already covers it):
      1. Price within 1.5% of a 4h or 1h swing low
      2. 4h stoch_k < 45  — room to run to resistance, not already stretched
      3. 15m bullish engulfing OR hammer
      4. Volume ≥ 0.8× vol_ma  — relaxed; ranges are structurally quiet
      5. TP: nearest 4h swing high (opposite side of range)
      6. RR ≥ 1.5

    SELL at resistance:
      1. Price within 1.5% of a 4h or 1h swing high
      2. 4h stoch_k > 65  — properly overbought, not just mildly elevated
      3. 15m shooting star OR bearish engulfing (current or prior candle)
      4. Volume ≥ 0.9×
      5. TP: nearest 4h swing low
      6. RR ≥ 1.5
    """
    if len(df_15m) < 3 or len(df_4h) < 3 or len(df_1h) < 3:
        return None

    last_4h = df_4h.iloc[-1]
    last    = df_15m.iloc[-1]
    prev    = df_15m.iloc[-2]

    atr = last['atr']
    if pd.isna(atr) or atr <= 0:
        return None

    entry = last['close']

    # Collect structural levels from 4h (primary) and 1h (secondary)
    lows_4h   = swing_lows(df_4h)
    highs_4h  = swing_highs(df_4h)
    lows_1h   = swing_lows(df_1h)
    highs_1h  = swing_highs(df_1h)

    support_candidates    = [l for l in lows_4h + lows_1h  if l < entry * 0.999]
    resistance_candidates = [h for h in highs_4h + highs_1h if h > entry * 1.001]

    nearest_sup = max(support_candidates)    if support_candidates    else None
    nearest_res = min(resistance_candidates) if resistance_candidates else None

    # ATR-based tolerance — markets don't respect perfect lines.
    # ATR×0.5 scales with the pair's volatility; a fixed 1.5% was too tight in fast markets.
    level_tol       = atr * 0.5
    near_support    = nearest_sup is not None and abs(entry - nearest_sup) <= level_tol
    near_resistance = nearest_res is not None and abs(entry - nearest_res) <= level_tol

    # Volume gate — must show some real participation, not dead-air candles
    if last['volume'] < last['vol_ma'] * 0.9:
        return None

    # ── BUY at support ─────────────────────────────────────────────────────
    # Bear mode disabled — existing bounce entry covers oversold support plays.
    # stoch_k < 35: must be properly oversold, not just approaching support mid-range.
    if near_support and market_mode != "bear" and last_4h['stoch_k'] < 35:
        body       = abs(last['close'] - last['open'])
        lower_wick = min(last['open'], last['close']) - last['low']
        upper_wick = last['high'] - max(last['open'], last['close'])
        is_hammer  = body > 0 and lower_wick >= 2 * body and upper_wick <= body

        if is_engulfing(df_15m, "BUY") or is_hammer:
            sl   = nearest_sup - (0.3 * atr)
            risk = entry - sl
            if risk > 0:
                tp1_candidates = [h for h in highs_4h if h > entry * 1.001]
                tp1 = min(tp1_candidates) if tp1_candidates else nearest_resistance(df_1h, entry)
                if tp1 is not None:
                    reward = tp1 - entry
                    if reward > 0:
                        rr = round(reward / risk, 2)
                        if rr >= 1.5:
                            tp2 = second_resistance(df_1h, tp1)
                            return "BUY", entry, sl, tp1, tp2, rr, atr, "range"

    # ── SELL at resistance ─────────────────────────────────────────────────
    # stoch_k > 65: must be properly overbought at resistance, not just elevated.
    if near_resistance and last_4h['stoch_k'] > 65:
        def _is_shooting_star(c):
            body       = abs(c['close'] - c['open'])
            upper_wick = c['high'] - max(c['open'], c['close'])
            lower_wick = min(c['open'], c['close']) - c['low']
            return body > 0 and upper_wick >= 2 * body and lower_wick <= body

        bearish_candle = (
            is_engulfing(df_15m, "SELL")
            or _is_shooting_star(last)
            or _is_shooting_star(prev)
        )

        if bearish_candle:
            sl   = nearest_res + (0.3 * atr)
            risk = sl - entry
            if risk > 0:
                tp1_candidates = [l for l in lows_4h if l < entry * 0.999]
                tp1 = max(tp1_candidates) if tp1_candidates else nearest_support(df_1h, entry)
                if tp1 is not None:
                    reward = entry - tp1
                    if reward > 0:
                        rr = round(reward / risk, 2)
                        if rr >= 1.5:
                            tp2 = second_support(df_1h, tp1)
                            return "SELL", entry, sl, tp1, tp2, rr, atr, "range"

    return None


# ==============================
# TREND ENTRY SIGNAL
# ==============================
def entry_signal_trend(df_15m, df_1h, df_4h, direction, params, market_mode="normal"):
    if len(df_15m) < 3 or len(df_1h) < 4:
        return None

    last = df_15m.iloc[-1]
    prev = df_15m.iloc[-2]

    rr_min = params["rr_min"]
    atr    = last['atr']
    if pd.isna(atr) or atr <= 0:
        return None

    # Time-of-day: 01–07 UTC is the deep Asia / low-liquidity window globally.
    # Volume is structurally lower then — not because the setup is weak.
    hour_utc = _dt.utcnow().hour
    low_liquidity_window = 1 <= hour_utc <= 7

    # Strong 4h trend bypass for BB/coil gates.
    # When 4h ADX > 28 the trend is well-established — breakouts are continuations
    # off pullbacks, not from coils. Coil quality only matters in borderline trends.
    strong_4h_trend = params.get("adx_4h", 0) > 28

    if direction == "BUY":
        # Explosive breakout trigger: close must clear previous candle's high
        if last['close'] <= prev['high']:
            return None

        # StochRSI gate raised to 85 — stoch_k 72–85 in an uptrend is normal
        # momentum, not overextension. Only gate at genuinely extreme levels.
        if last['stoch_k'] > 85:
            strong_breakout = (last['close'] - prev['high']) > 0.3 * atr
            strong_volume   = last['volume'] > last['vol_ma'] * 1.5
            if not (strong_breakout and strong_volume):
                return None

        # MACD turning bullish over 3 bars — catches the momentum shift before the
        # full zero-line cross. Best entries are at the turn, not after it.
        if df_1h['macd_hist'].iloc[-1] <= df_1h['macd_hist'].iloc[-4]:
            return None

        # Volume — time-of-day aware:
        #   Low liquidity window (01–07 UTC): 0.8× — structurally thin, not weak setup
        #   Strong breakout (close > prev_high by > 0.5 ATR): 1.0× — price = conviction
        #   Standard: 1.15×
        strong_breakout_move = (last['close'] - prev['high']) > 0.5 * atr
        vol_thresh = 0.8 if low_liquidity_window else (1.0 if strong_breakout_move else 1.15)
        if last['volume'] < last['vol_ma'] * vol_thresh:
            return None

        # Compression → expansion: coil before the breakout.
        # Skipped in HIGH vol (crash/breakout already underway — no coil forms).
        # Skipped when 4h ADX > 28 (strong trend continuation off a pullback).
        if not params.get("high_vol") and not strong_4h_trend:
            if not is_bb_squeeze(df_15m):
                return None
            if not consolidation_coil(df_15m, atr):
                return None

        entry  = last['close']
        sl_buf = 0.5 * atr if params.get("high_vol") else 0.3 * atr
        # Nearest confirmed 15m swing low as structural anchor; fall back to rolling min
        sw_lows = [l for l in swing_lows(df_15m) if l < entry]
        sl_level = max(sw_lows) if sw_lows else df_15m['low'].tail(20).min()
        sl    = sl_level - sl_buf
        risk  = entry - sl
        if risk <= 0 or risk < atr * 0.4:
            return None

        # TP1: prefer nearest 1h swing high; fall back to 4h when 1h is too close
        tp1 = nearest_resistance(df_1h, entry)
        if tp1 is not None:
            if round((tp1 - entry) / risk, 2) < rr_min:
                tp1_4h = nearest_resistance(df_4h, entry)
                if tp1_4h is not None and tp1_4h > tp1:
                    tp1 = tp1_4h
        else:
            tp1 = nearest_resistance(df_4h, entry)
        if tp1 is None:
            return None
        reward = tp1 - entry

    elif direction == "SELL":
        # Explosive breakdown trigger: close must break below previous candle's low
        if last['close'] >= prev['low']:
            return None

        # MACD turning bearish over 3 bars
        if df_1h['macd_hist'].iloc[-1] >= df_1h['macd_hist'].iloc[-4]:
            return None

        # Volume — time-of-day aware with bear mode override:
        #   Bear mode: 0.80× low-liquidity, 0.90× standard (panic thins volume market-wide)
        #   Normal/recovery: 0.80× low-liquidity, 1.0× strong breakdown, 1.15× standard
        strong_breakdown_move = (prev['low'] - last['close']) > 0.5 * atr
        if market_mode == "bear":
            vol_thresh = 0.80 if low_liquidity_window else 0.90
        else:
            vol_thresh = 0.80 if low_liquidity_window else (1.0 if strong_breakdown_move else 1.15)
        if last['volume'] < last['vol_ma'] * vol_thresh:
            return None

        # Bear mode: skip BB squeeze and coil — crashes move in steps, not coils.
        # HIGH vol already bypasses these; strong_4h_trend bypass mirrors BUY logic.
        if market_mode != "bear" and not params.get("high_vol") and not strong_4h_trend:
            if not is_bb_squeeze(df_15m):
                return None
            if not consolidation_coil(df_15m, atr):
                return None

        entry  = last['close']
        sl_buf = 0.5 * atr if params.get("high_vol") else 0.3 * atr
        # Nearest confirmed 15m swing high as structural anchor; fall back to rolling max
        sw_highs = [h for h in swing_highs(df_15m) if h > entry]
        sl_level = min(sw_highs) if sw_highs else df_15m['high'].tail(20).max()
        sl    = sl_level + sl_buf
        risk  = sl - entry
        if risk <= 0 or risk < atr * 0.4:
            return None

        # TP1: prefer nearest 1h swing low; fall back to 4h when 1h is too close
        tp1 = nearest_support(df_1h, entry)
        if tp1 is not None:
            if round((entry - tp1) / risk, 2) < rr_min:
                tp1_4h = nearest_support(df_4h, entry)
                if tp1_4h is not None and tp1_4h < tp1:
                    tp1 = tp1_4h
        else:
            tp1 = nearest_support(df_4h, entry)
        if tp1 is None:
            return None
        reward = entry - tp1

    else:
        return None

    if reward <= 0:
        return None

    rr_raw = reward / risk

    # ATR cap: swing levels further than 3.5R are unlikely to be reached before
    # reversal (backtest: only 2/48 trades hit TP1 at the full swing target).
    # When RR > 3.5, cap TP1 at 2.5×ATR — a realistic near-term target — and
    # promote the original swing level to TP2 as the runner.
    _ATR_TP_MULT = 2.5
    _ATR_CAP_RR  = 3.5
    if rr_raw > _ATR_CAP_RR:
        tp2 = tp1
        tp1 = (entry + _ATR_TP_MULT * atr) if direction == "BUY" else (entry - _ATR_TP_MULT * atr)
        reward = abs(tp1 - entry)
    else:
        tp2 = second_resistance(df_1h, tp1) if direction == "BUY" else second_support(df_1h, tp1)

    rr = round(reward / risk, 2)
    if rr > 6.5:
        return None  # SL too tight for market noise

    if rr >= rr_min:
        pass  # TP1 alone is sufficient
    elif tp2 is not None:
        tp2_rr = round(abs(tp2 - entry) / risk, 2)
        if tp2_rr < rr_min or rr < 1.5:
            return None
    else:
        return None

    return direction, entry, sl, tp1, tp2, rr, atr, "trend"


# ==============================
# REVERSAL ENTRY SIGNAL
# ==============================
def entry_signal_reversal(df_15m, df_1h, df_4h, direction, params):
    """
    Reversal entries require engulfing pattern + extreme StochRSI + strong volume.
    SL is placed behind the engulfing candle itself — the candle defines the
    invalidation point. TP1 is the nearest structural level on 1h, with 4h
    fallback when 1h is too close. TP2 is the next structural level.
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

        entry  = last['close']
        sl_buf = 0.5 * atr if params.get("high_vol") else 0.3 * atr
        sl     = last['low'] - sl_buf
        risk   = entry - sl
        if risk <= 0 or risk < atr * 0.5:
            return None

        # TP1: prefer nearest 1h swing high; fall back to 4h when 1h is too close
        tp1 = nearest_resistance(df_1h, entry)
        if tp1 is not None:
            if round((tp1 - entry) / risk, 2) < rr_min:
                tp1_4h = nearest_resistance(df_4h, entry)
                if tp1_4h is not None and tp1_4h > tp1:
                    tp1 = tp1_4h
        else:
            tp1 = nearest_resistance(df_4h, entry)
        if tp1 is None:
            return None
        reward = tp1 - entry

    elif direction == "SELL":
        if last['stoch_k'] < stoch_ob - 10:         # Must come from overbought zone
            return None
        if last['volume'] < last['vol_ma'] * 1.3:
            return None

        entry  = last['close']
        sl_buf = 0.5 * atr if params.get("high_vol") else 0.3 * atr
        sl     = last['high'] + sl_buf
        risk   = sl - entry
        if risk <= 0 or risk < atr * 0.5:
            return None

        # TP1: prefer nearest 1h swing low; fall back to 4h when 1h is too close
        tp1 = nearest_support(df_1h, entry)
        if tp1 is not None:
            if round((entry - tp1) / risk, 2) < rr_min:
                tp1_4h = nearest_support(df_4h, entry)
                if tp1_4h is not None and tp1_4h < tp1:
                    tp1 = tp1_4h
        else:
            tp1 = nearest_support(df_4h, entry)
        if tp1 is None:
            return None
        reward = entry - tp1

    else:
        return None

    if reward <= 0:
        return None

    rr_raw = reward / risk

    # ATR cap: same logic as trend — cap TP1 at 2.5×ATR when swing target > 3.5R,
    # promote original swing level to TP2 as runner.
    _ATR_TP_MULT = 2.5
    _ATR_CAP_RR  = 3.5
    if rr_raw > _ATR_CAP_RR:
        tp2 = tp1
        tp1 = (entry + _ATR_TP_MULT * atr) if direction == "BUY" else (entry - _ATR_TP_MULT * atr)
        reward = abs(tp1 - entry)
    else:
        tp2 = second_resistance(df_1h, tp1) if direction == "BUY" else second_support(df_1h, tp1)

    rr = round(reward / risk, 2)
    if rr > 6.5:
        return None  # SL too tight for market noise

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

    # Trending vs ranging routing — slope-aware.
    #
    # Three-zone decision:
    #   Zone A (ADX >= adx_route)            → trending, no slope needed
    #   Zone B (ADX in [adx_route-3, adx_route) AND slope >= +0.5/3bars)
    #                                         → trending — ADX building toward threshold,
    #                                           trend is forming not fading
    #   Zone C (everything else)              → ranging — route to S/R entry
    #
    # Using slope here distinguishes "ADX 16 and rising" (trend initiating)
    # from "ADX 16 and falling" (trend collapsing back to range) at the boundary.
    adx_val   = df_4h.iloc[-1]['adx']
    adx_slope = float(df_4h['adx'].iloc[-1] - df_4h['adx'].iloc[-4]) if len(df_4h) >= 4 else 0.0
    adx_route = params["adx_route"]

    trending = (
        adx_val >= adx_route                                          # Zone A
        or (adx_val >= adx_route - 3 and adx_slope >= 0.5)           # Zone B
    )

    # Ranging market — range and bounce entries disabled (backtest showed net negative).
    # Range: 61% WR but avg win only ~0.49R vs -1.0R loss → TP targeting broken.
    # Bounce: 40.5% WR — direction wrong more often than right on this setup.
    # Both will be reworked separately. For now skip and log.
    if not trending:
        print(f"  ↳ {symbol}: ADX {adx_val:.1f} slope {adx_slope:+.1f} <{adx_route} [{regime_label}] ranging — skip (range/bounce disabled)")
        return None

    # Bear mode fade at resistance — kept, targets well-defined structural level.
    near_ema50_resistance = False
    if market_mode == "bear":
        _ema50_4h   = df_4h.iloc[-1]['ema50']
        _last_close = df_15m.iloc[-1]['close']
        near_ema50_resistance = abs(_last_close - _ema50_4h) / _ema50_4h <= 0.015

    if near_ema50_resistance:
        fade = entry_signal_fade_resistance(df_15m, df_4h, df_1h, params)
        if fade:
            direction, entry, sl, tp1, tp2, rr, atr, trade_type = fade
            return direction, entry, sl, tp1, tp2, rr, atr, trade_type

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
        result = entry_signal_reversal(df_15m, df_1h, df_4h, reversal, params)
        if result:
            direction, entry, sl, tp1, tp2, rr, atr, trade_type = result
            return direction, entry, sl, tp1, tp2, rr, atr, trade_type
        print(f"  ↳ {symbol}: reversal {reversal} detected but entry conditions not met")

    # Trend following
    bias = get_htf_bias(df_1h, df_4h, df_1d, params, market_mode)
    if not bias:
        last_1h = df_1h.iloc[-1]
        last_4h = df_4h.iloc[-1]
        # DI mandatory + 3/4 remaining (2/4 HIGH vol); bear drops threshold by 1
        base_threshold = 2 if params.get("high_vol") else 3
        threshold = max(base_threshold - 1, 1) if market_mode == "bear" else base_threshold
        di_dir = "bull" if last_4h['plus_di'] > last_4h['minus_di'] else "bear"
        print(f"  ↳ {symbol}: HTF bias DI={di_dir}, score <{threshold}/4 [{regime_label}] ema50={'>' if last_1h['ema50'] > last_1h['ema200'] else '<'}ema200")
        return None

    result = entry_signal_trend(df_15m, df_1h, df_4h, bias, params, market_mode)
    if result:
        direction, entry, sl, tp1, tp2, rr, atr, trade_type = result
        return direction, entry, sl, tp1, tp2, rr, atr, trade_type

    # Log why trend entry was rejected
    last    = df_15m.iloc[-1]
    prev    = df_15m.iloc[-2]
    atr     = last['atr']
    hour_utc = _dt.utcnow().hour
    low_liquidity_window = 1 <= hour_utc <= 7
    strong_4h_trend = params.get("adx_4h", 0) > 28
    reasons = []

    if bias == "BUY":
        if last['close'] <= prev['high']:
            reasons.append("no breakout")
        if last['stoch_k'] > 85:
            strong_breakout = (last['close'] - prev['high']) > 0.3 * atr
            strong_volume   = last['volume'] > last['vol_ma'] * 1.5
            if not (strong_breakout and strong_volume):
                reasons.append(f"stoch OB {last['stoch_k']:.0f}>85 weak-breakout")
        if len(df_1h) >= 4 and df_1h['macd_hist'].iloc[-1] <= df_1h['macd_hist'].iloc[-4]:
            reasons.append("1h MACD not turning bull")
        strong_breakout_move = (last['close'] - prev['high']) > 0.5 * atr
        vol_thresh = 0.8 if low_liquidity_window else (1.0 if strong_breakout_move else 1.15)
        if last['volume'] < last['vol_ma'] * vol_thresh:
            reasons.append(f"vol low {last['volume']/last['vol_ma']:.2f}x (need {vol_thresh}x)")
        if not params.get("high_vol") and not strong_4h_trend:
            if not is_bb_squeeze(df_15m):
                reasons.append("no BB squeeze")
            if not consolidation_coil(df_15m, atr):
                reasons.append("no coil")
    else:
        if last['close'] >= prev['low']:
            reasons.append("no breakdown")
        if len(df_1h) >= 4 and df_1h['macd_hist'].iloc[-1] >= df_1h['macd_hist'].iloc[-4]:
            reasons.append("1h MACD not turning bear")
        strong_breakdown_move = (prev['low'] - last['close']) > 0.5 * atr
        if market_mode == "bear":
            vol_thresh = 0.80 if low_liquidity_window else 0.90
        else:
            vol_thresh = 0.80 if low_liquidity_window else (1.0 if strong_breakdown_move else 1.15)
        if last['volume'] < last['vol_ma'] * vol_thresh:
            reasons.append(f"vol low {last['volume']/last['vol_ma']:.2f}x (need {vol_thresh}x)")
        if market_mode != "bear" and not params.get("high_vol") and not strong_4h_trend:
            if not is_bb_squeeze(df_15m):
                reasons.append("no BB squeeze")
            if not consolidation_coil(df_15m, atr):
                reasons.append("no coil")
    print(f"  ↳ {symbol}: {bias} entry rejected [{regime_label}] — {', '.join(reasons) if reasons else 'RR/TP failed'}")

    return None


# ==============================
# PULLBACK TREND SIGNAL
# ==============================
def generate_pullback_signal(df_15m, df_1h, df_4h, df_1d=None, symbol="", market_mode="normal"):
    """
    Routing logic (ADX ≥ threshold branch):

      trend_ok  = 4h EMA50 slope clearly bull or bear
      rsi_ok    = 1h RSI in pullback zone
      confluence = Step-3 trigger score (0-3)

      trend_ok AND rsi_ok AND confluence ≥ 2  →  PULLBACK
      trend_ok                                →  BOUNCE
      else                                    →  MICRO

    ADX < threshold → RANGE → else MICRO (unchanged)
    Bear mode: only SELL; recovery mode: no SELL.
    """
    if len(df_4h) < 6 or len(df_1h) < 2 or len(df_15m) < 3:
        return None

    last_4h  = df_4h.iloc[-1]
    last_1h  = df_1h.iloc[-1]
    last_15m = df_15m.iloc[-1]

    params = get_regime_params(df_4h, market_mode)

    # ── ADX gate: ranging market → RANGE → else MICRO ─────────────────────
    adx = last_4h.get("adx", 0)
    adx_min = params["adx_route"]  # regime-adaptive: 15 HIGH / 18 NORMAL / 20 LOW (bear -2)
    if pd.isna(adx) or adx < adx_min:
        range_result = entry_signal_range(df_15m, df_1h, df_4h, params, market_mode)
        if range_result:
            direction, entry, sl, tp1, tp2, rr, atr, _ = range_result
            print(f"  ✅ RANGE {direction} {symbol} | ADX4h={adx:.1f}<{adx_min} (ranging) | entry={entry:.4f} sl={sl:.4f} tp1={tp1:.4f} rr={rr} | mode={market_mode}")
            return range_result
        micro = entry_signal_micro_trend(df_15m, df_1h, params, market_mode)
        if micro:
            m_dir, m_entry, m_sl, m_tp1, _, m_rr, m_atr, _ = micro
            print(f"  ✅ MICRO {m_dir} {symbol} | ADX4h={adx:.1f} (range miss) | entry={m_entry:.4f} sl={m_sl:.4f} tp1={m_tp1:.4f} rr={m_rr} | mode={market_mode}")
        return micro

    # ── trend_ok: 4h EMA50 slope clearly directional ──────────────────────
    ema50_now  = last_4h["ema50"]
    ema200_now = last_4h["ema200"]
    ema50_prev = df_4h.iloc[-6]["ema50"]
    if any(pd.isna(x) for x in [ema50_now, ema200_now, ema50_prev]):
        return None

    ema_bull = (ema50_now > ema200_now) and (ema50_now > ema50_prev)
    ema_bear = (ema50_now < ema200_now) and (ema50_now < ema50_prev)

    if market_mode == "bear":
        if not ema_bear:
            return None       # bear mode: only trade SELL in a confirmed downtrend
        direction = "SELL"
        trend_ok  = True
    elif ema_bull and not ema_bear:
        direction = "BUY"
        trend_ok  = True
    elif ema_bear and not ema_bull:
        direction = "SELL"
        trend_ok  = True
    else:
        direction = None      # flat / conflicting — no pullback direction
        trend_ok  = False

    # Recovery mode: no shorting into a recovering market
    if market_mode == "recovery" and direction == "SELL":
        return None

    # ── rsi_ok: 1h RSI in pullback zone ───────────────────────────────────
    rsi_1h = last_1h.get("rsi")
    if pd.isna(rsi_1h):
        return None

    if trend_ok:
        rsi_ok = (40 <= rsi_1h <= 48) if direction == "BUY" else (52 <= rsi_1h <= 60)
    else:
        rsi_ok = False

    # ── confluence: Step-3 trigger score ──────────────────────────────────
    confluence = 0
    if trend_ok and rsi_ok and len(df_1h) >= 5:
        prev_1h     = df_1h.iloc[-2]
        rsi_1h_prev = prev_1h.get("rsi")
        ema20       = last_15m.get("ema20")
        close       = last_15m["close"]
        if not any(pd.isna(x) for x in [rsi_1h_prev, ema20]):
            if direction == "BUY":
                rsi_in_zone = 40 <= rsi_1h <= 52
                rsi_cross   = (rsi_1h_prev <= 45) and (rsi_1h > 45)
                ema_align   = close > ema20
            else:
                rsi_in_zone = 48 <= rsi_1h <= 60
                rsi_cross   = (rsi_1h_prev >= 55) and (rsi_1h < 55)
                ema_align   = close < ema20
            confluence = sum([rsi_in_zone, rsi_cross, ema_align])

    # ── Route ─────────────────────────────────────────────────────────────
    if trend_ok and rsi_ok and confluence >= 2:
        # Full pullback setup confirmed
        atr   = last_15m.get("atr", 0)
        close = last_15m["close"]
        if direction == "BUY":
            sl = df_15m["low"].tail(10).min() - 0.3 * atr
        else:
            sl = df_15m["high"].tail(10).max() + 0.3 * atr
        risk = abs(close - sl)
        if risk <= 0 or risk > close * 0.06:
            return None
        # Use structural TP (nearest 1h swing level) instead of fixed 1R
        if direction == "BUY":
            tp1 = nearest_resistance(df_1h, close) or nearest_resistance(df_4h, close)
            tp2 = second_resistance(df_1h, tp1) if tp1 else None
        else:
            tp1 = nearest_support(df_1h, close) or nearest_support(df_4h, close)
            tp2 = second_support(df_1h, tp1) if tp1 else None
        if tp1 is None:
            return None
        reward = abs(tp1 - close)
        rr = round(reward / risk, 2)
        if rr < 1.5:
            return None
        print(f"  ✅ PULLBACK {direction} {symbol} | RSI1h={rsi_1h:.1f} ADX4h={adx:.1f} conf={confluence}/3 | entry={close:.4f} sl={sl:.4f} tp1={tp1:.4f} rr={rr} | mode={market_mode}")
        return (direction, close, sl, tp1, tp2, rr, atr, "pullback")

    elif trend_ok:
        # Trend is clear but RSI not in zone or confluence too low → BOUNCE
        reason = "rsi-zone-miss" if not rsi_ok else "low-conf"
        bounce_result = entry_signal_bounce(df_15m, df_1h, df_4h, params)
        if bounce_result:
            b_dir, b_entry, b_sl, b_tp1, b_tp2, b_rr, b_atr, _ = bounce_result
            print(f"  ✅ BOUNCE {b_dir} {symbol} | RSI1h={rsi_1h:.1f} ADX4h={adx:.1f} ({reason}→bounce) | entry={b_entry:.4f} sl={b_sl:.4f} tp1={b_tp1:.4f} rr={b_rr} | mode={market_mode}")
        return bounce_result

    else:
        # EMA flat / conflicting — no trend direction → MICRO
        micro = entry_signal_micro_trend(df_15m, df_1h, params, market_mode)
        if micro:
            m_dir, m_entry, m_sl, m_tp1, _, m_rr, m_atr, _ = micro
            _rsi_log = rsi_1h or 0
            print(f"  ✅ MICRO {m_dir} {symbol} | RSI1h={_rsi_log:.1f} ADX4h={adx:.1f} (ema-flat→micro) | entry={m_entry:.4f} sl={m_sl:.4f} tp1={m_tp1:.4f} rr={m_rr} | mode={market_mode}")
        return micro
