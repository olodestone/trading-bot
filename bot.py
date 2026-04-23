import ccxt
import pandas as pd
import time
from datetime import datetime, timedelta
import os
import signal as signal_module

from strategy import apply_indicators, generate_pullback_signal
from performance import (
    save_trade, check_trade_results, daily_report,
    ensure_csv, save_pending_trades, load_pending_trades,
    get_daily_losses, get_engine, TRADES_TABLE, get_stats_summary,
    get_compounded_balance,
)
from logger import send_telegram, send_csv, get_updates, TOKEN, CHAT_ID


# ==============================
# EXCHANGES
# ==============================
spot_exchange = ccxt.kucoin({"enableRateLimit": True, "rateLimit": 1200, "timeout": 10000})
futures_exchange = ccxt.mexc({"enableRateLimit": True, "timeout": 10000})

spot_exchange.options['adjustForTimeDifference'] = True
futures_exchange.options['adjustForTimeDifference'] = True
futures_exchange.options['defaultType'] = 'swap'   # ensure futures tickers, not spot

SPOT_MARKETS = spot_exchange.load_markets()
FUTURES_MARKETS = futures_exchange.load_markets()

MARKET_REFRESH_INTERVAL = 86400  # 24 hours
last_market_refresh = time.time()

# Ticker cache — persists last successful fetch_tickers() result per exchange.
# Used as fallback when the live call times out, giving volume-ranked pair data
# instead of the raw alphabetical market list.
_tickers_cache      = {"spot": None, "futures": None}
_tickers_cache_time = {"spot": 0.0,  "futures": 0.0}


# ==============================
# MARKET REFRESH
# ==============================
def refresh_markets_if_needed():
    global SPOT_MARKETS, FUTURES_MARKETS, last_market_refresh
    if time.time() - last_market_refresh > MARKET_REFRESH_INTERVAL:
        try:
            SPOT_MARKETS = spot_exchange.load_markets()
            FUTURES_MARKETS = futures_exchange.load_markets()
            last_market_refresh = time.time()
            print("🔄 Markets refreshed")
        except Exception as e:
            print(f"Market refresh error: {e}")


# ==============================
# CACHE
# ==============================
HTF_CACHE = {}
HTF_LAST_UPDATE = {}
MARKET_DATA = {}
PRICE_CACHE = {}

HTF_REFRESH = {"1h": 1200, "4h": 14400, "1d": 86400}

# ==============================
# MARKET MODE STATE
# ==============================
# bear_breadth = % of top-20 pairs where ema50 < ema200 on 1h.
# Tracks consecutive scans in each zone to debounce noise.
_bear_mode_scans = 0       # consecutive scans with breadth > 65%
_recovery_scans  = 0       # consecutive scans with breadth < 45%
_market_mode     = "normal" # "bear" | "recovery" | "normal"


def _update_market_mode(breadth_data):
    """
    Compute bear_breadth from already-computed 1h indicator data.
    bear_breadth = fraction of pairs where ema50 < ema200 on 1h.

    Bear mode activates when > 65% of pairs are below their 200 EMA
    for 2+ consecutive scans. Deactivates when breadth drops below 45%
    for 3+ consecutive scans (recovery mode).

    Bear mode   → SELL only, ADX gate -3, vol threshold 0.90×, coil/BB skipped
    Recovery    → All signals, RR minimum 2.0 across all regimes
    """
    global _bear_mode_scans, _recovery_scans, _market_mode

    if not breadth_data:
        return

    bear_count = sum(
        1 for df_1h in breadth_data.values()
        if df_1h.iloc[-1]['ema50'] < df_1h.iloc[-1]['ema200']
    )
    breadth = bear_count / len(breadth_data)

    if breadth > 0.65:
        _bear_mode_scans += 1
        _recovery_scans   = 0
    elif breadth <= 0.45:
        _recovery_scans  += 1
        _bear_mode_scans  = 0
    else:
        _bear_mode_scans = max(0, _bear_mode_scans - 1)
        _recovery_scans  = max(0, _recovery_scans  - 1)

    prev_mode = _market_mode
    if _bear_mode_scans >= 2:
        _market_mode = "bear"
    elif _recovery_scans >= 3:
        _market_mode = "recovery"
    else:
        _market_mode = "normal"

    mode_labels = {"bear": "🐻 BEAR", "recovery": "🌱 RECOVERY", "normal": "😐 NORMAL"}
    print(f"📊 Market mode: {mode_labels[_market_mode]} | bear_breadth: {breadth:.0%} ({bear_count}/{len(breadth_data)} pairs ema50<ema200)")

    # Notify on mode transitions
    if _market_mode != prev_mode:
        if _market_mode == "bear":
            send_telegram(
                f"🐻 BEAR MODE ACTIVATED\n"
                f"bear_breadth: {breadth:.0%} ({bear_count}/{len(breadth_data)} pairs below ema200)\n\n"
                f"Changes active:\n"
                f"• SELL signals on confirmed 4h downtrend pairs only\n"
                f"• Bounce BUY at structural support still enabled\n"
                f"• Fade-resistance SELL at 4h EMA50 enabled\n"
                f"• ADX gate −2 (normal/LOW vol) or −3 (HIGH vol)\n"
                f"• Volume threshold → 0.80× low-liquidity / 0.90× standard\n"
                f"• BB squeeze & coil skipped for SELL\n"
                f"• HTF bias threshold reduced by 1"
            )
        elif _market_mode == "recovery":
            send_telegram(
                f"🌱 RECOVERY MODE ACTIVATED\n"
                f"bear_breadth: {breadth:.0%} ({bear_count}/{len(breadth_data)} pairs below ema200)\n\n"
                f"Changes active:\n"
                f"• BUY signals only (SELL blocked across all entry types)\n"
                f"• RR minimum → 2.0 (all regimes)\n"
                f"• Bounce BUY: candle required + BOTH price above 1h EMA50 AND higher low\n"
                f"• Bounce BUY SL buffer: 0.5×ATR (wider, survives wicks)"
            )
        elif prev_mode in ("bear", "recovery"):
            send_telegram(
                f"😐 NORMAL MODE RESTORED\n"
                f"bear_breadth: {breadth:.0%}\n"
                f"All standard parameters in effect."
            )


def get_cached_tf(symbol, tf, market_type):
    key = f"{symbol}_{tf}_{market_type}"
    now = time.time()
    refresh = HTF_REFRESH.get(tf, 0)
    if key in HTF_CACHE and (now - HTF_LAST_UPDATE.get(key, 0) < refresh):
        return HTF_CACHE[key]
    df, _ = fetch_tf(symbol, tf, market_type)
    if df is not None:
        HTF_CACHE[key] = df
        HTF_LAST_UPDATE[key] = now
    return df


# ==============================
# PENDING TRADES + DEDUP
# ==============================
pending_trades = []
last_signals = {}  # key: "pair_signal_date" → True


def is_new_signal(pair, sig):
    today = str(datetime.now().date())
    key = f"{pair}_{sig}_{today}"
    if key in last_signals:
        return False
    last_signals[key] = True
    return True


def prune_last_signals():
    """Remove entries from previous days."""
    today = str(datetime.now().date())
    stale = [k for k in last_signals if not k.endswith(today)]
    for k in stale:
        del last_signals[k]


def _restore_last_signals():
    """Re-populate last_signals from today's DB trades after a restart."""
    today = str(datetime.utcnow().date())
    try:
        engine = get_engine()
        df = pd.read_sql(
            f"SELECT pair, signal FROM {TRADES_TABLE} WHERE time >= %(d)s",
            engine, params={"d": today}
        )
        for _, row in df.iterrows():
            last_signals[f"{row['pair']}_{row['signal']}_{today}"] = True
    except Exception:
        pass
    for t in pending_trades:
        last_signals[f"{t['pair']}_{t['signal']}_{today}"] = True


def _had_tp1_hit_today(symbol):
    """True if the most recent closed trade for this pair today had tp1_hit=True."""
    try:
        engine = get_engine()
        today = str(datetime.utcnow().date())
        df = pd.read_sql(
            f"SELECT tp1_hit FROM {TRADES_TABLE} "
            f"WHERE pair = %(pair)s AND status != 'OPEN' AND time >= %(today)s "
            f"ORDER BY time DESC LIMIT 1",
            engine, params={"pair": symbol, "today": today}
        )
        if df.empty:
            return False
        val = df.iloc[0]['tp1_hit']
        return bool(val) if val is not None and not pd.isna(val) else False
    except Exception:
        return False


# ==============================
# CAPACITY + DAILY LOSS GUARDS
# ==============================
MAX_CONCURRENT = 10
MAX_DAILY_LOSSES = 5

# Mid-cap price filter — focus on explosive movers, exclude BTC/ETH and sub-cent noise

# ==============================
# POSITION SIZING CONFIG
# Set ACCOUNT_BALANCE and RISK_PCT as env vars, or edit defaults here.
# RISK_PCT = 0.02 means risk 2% of account per trade.
# ==============================
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "15"))
STARTING_BALANCE = ACCOUNT_BALANCE   # fixed origin for compounding calculation
RISK_PCT = float(os.getenv("RISK_PCT", "0.02"))


def at_max_capacity():
    open_count = 0
    try:
        engine = get_engine()
        df = pd.read_sql(
            f"SELECT COUNT(*) as cnt FROM {TRADES_TABLE} WHERE status = 'OPEN'", engine
        )
        open_count = int(df['cnt'].iloc[0])
    except Exception:
        pass
    return len(pending_trades) + open_count >= MAX_CONCURRENT


def daily_loss_limit_hit():
    return get_daily_losses() >= MAX_DAILY_LOSSES


def _in_loss_cooldown():
    """Block new signal generation for 15 min after any stop loss."""
    try:
        engine = get_engine()
        cutoff = str(datetime.utcnow() - timedelta(minutes=15))
        df = pd.read_sql(
            f"SELECT COUNT(*) as cnt FROM {TRADES_TABLE} WHERE status = 'LOSS' AND time >= %(c)s",
            engine, params={"c": cutoff}
        )
        return int(df['cnt'].iloc[0]) > 0
    except Exception:
        return False


def _directional_count(sig):
    """Open + pending trades in the given direction (BUY or SELL)."""
    try:
        engine = get_engine()
        df = pd.read_sql(
            f"SELECT COUNT(*) as cnt FROM {TRADES_TABLE} WHERE status = 'OPEN' AND signal = %(s)s",
            engine, params={"s": sig}
        )
        open_count = int(df['cnt'].iloc[0])
    except Exception:
        open_count = 0
    return open_count + sum(1 for t in pending_trades if t['signal'] == sig)


# ==============================
# TELEGRAM COMMAND HANDLING
# ==============================
last_update_id = 0


def check_telegram_commands():
    global pending_trades, last_update_id
    updates = get_updates(last_update_id + 1)
    for update in updates:
        last_update_id = update["update_id"]
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat = str(msg.get("chat", {}).get("id", ""))

        if chat != str(CHAT_ID):
            continue  # ignore messages from other chats

        if text == "/status":
            _handle_status()
        elif text == "/stats":
            send_telegram(get_stats_summary())
        elif text == "/help":
            send_telegram(
                "📖 Commands:\n"
                "/status — open & pending trades\n"
                "/stats  — win rate, expectancy, all-time edge\n"
                "/cancel SYMBOL — remove pending signal\n"
                "/help — this message"
            )
        elif text.startswith("/cancel "):
            symbol = text.split(" ", 1)[1].strip().upper()
            _handle_cancel(symbol)


def _handle_status():
    try:
        engine = get_engine()
        df = pd.read_sql(
            f"SELECT pair, signal, entry, rr FROM {TRADES_TABLE} WHERE status = 'OPEN'", engine
        )
        open_list = (
            "\n".join(f"  {r['pair']} {r['signal']} @ {r['entry']:.6f} RR:{r['rr']}"
                      for _, r in df.iterrows())
            or "  None"
        )
        pend_list = (
            "\n".join(f"  {t['pair']} {t['signal']} @ {t['entry']:.6f}"
                      for t in pending_trades)
            or "  None"
        )
        send_telegram(f"📊 STATUS\n\nOpen:\n{open_list}\n\nPending:\n{pend_list}")
    except Exception as e:
        send_telegram(f"Status error: {e}")


def _handle_cancel(symbol):
    global pending_trades
    before = len(pending_trades)
    pending_trades = [t for t in pending_trades if t['pair'] != symbol]
    if len(pending_trades) < before:
        save_pending_trades(pending_trades)
        send_telegram(f"✅ Cancelled pending: {symbol}")
    else:
        send_telegram(f"⚠️ No pending trade for: {symbol}")


# ==============================
# POSITION SIZING
# ==============================
def calc_position_size(entry, sl, rr=0.0):
    """
    Returns (risk_dollars, units, position_value, risk_pct).
    Risk scales with signal quality: base 2%, +0.5% per RR point above 3.0, capped at 4%.
    RR 2.5→2%  RR 3.0→2%  RR 3.5→2.5%  RR 4.0→3%  RR 5.0+→4%
    """
    risk_pct = min(RISK_PCT + max(0.0, rr - 3.0) * 0.005, 0.04)
    risk_dollars = round(ACCOUNT_BALANCE * risk_pct, 4)
    risk_per_unit = abs(entry - sl)
    if risk_per_unit <= 0:
        return risk_dollars, 0.0, 0.0, risk_pct
    units = risk_dollars / risk_per_unit
    position_value = round(units * entry, 4)
    return risk_dollars, round(units, 6), position_value, risk_pct


# ==============================
# PRICE FORMATTER
# ==============================
def _fmt_price(p):
    if p >= 1000:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:.4f}"
    elif p >= 0.01:
        return f"{p:.6f}"
    elif p >= 0.0001:
        return f"{p:.8f}"
    else:
        return f"{p:.10f}"


# ==============================
# FETCH
# ==============================
def timeout_handler(signum, frame):
    raise Exception("Timeout")


def fetch_tf(symbol, tf, market_type):
    key = f"{symbol}_{tf}_{market_type}"
    if key in MARKET_DATA:
        return MARKET_DATA[key]

    ex = spot_exchange if market_type == "spot" else futures_exchange

    for attempt in range(2):
        try:
            signal_module.signal(signal_module.SIGALRM, timeout_handler)
            signal_module.alarm(10)
            # fetch 200 candles so indicators (EMA200, ADX) have enough history
            data = ex.fetch_ohlcv(symbol, tf, limit=200)
            signal_module.alarm(0)

            df = pd.DataFrame(data, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            MARKET_DATA[key] = (df, ex.id)
            return df, ex.id

        except Exception as e:
            signal_module.alarm(0)
            if "429" in str(e):
                print("⚠️ Rate limit, cooling...")
                time.sleep(15)
            else:
                print(f"Fetch retry {attempt + 1} {symbol} {tf}: {e}")
                time.sleep(2)

    return None, None


# ==============================
# PRICE LOOKUP
# ==============================
def get_price(symbol, market_type):
    key = f"{symbol}_15m_{market_type}"
    if key in MARKET_DATA:
        df, _ = MARKET_DATA[key]
        if df is not None and not df.empty:
            return df.iloc[-1]['close']
    # MARKET_DATA not populated (e.g. max-capacity scan was skipped) — fetch live
    ex = spot_exchange if market_type == "spot" else futures_exchange
    try:
        ticker = ex.fetch_ticker(symbol)
        price = ticker.get('last') or ticker.get('close')
        return float(price) if price else None
    except Exception:
        return None


# ==============================
# MOMENTUM PAIR SELECTION
# ==============================
# STABLECOIN BLOCKLIST
# ==============================
_STABLES = {
    "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "FRAX", "USDP",
    "UST", "USDD", "SUSD", "GUSD", "LUSD", "PYUSD", "USDJ",
    "CUSD", "CEUR", "EURS", "ALUSD", "USDN", "MUSD", "USDX",
}

# Non-crypto commodity / metals perpetuals that appear on MEXC futures
_NON_CRYPTO = {
    "XAUT", "PAXG", "CACHE", "XAU", "XAG", "XAGT",        # gold / silver tokens
    "SILVER", "GOLD",                                       # metal perpetuals
    "USOIL", "UKOIL", "OIL", "BRENT", "WTI",               # oil perpetuals
    "WHEAT", "CORN", "SOYB",                                # agricultural
}


def _is_stable(symbol):
    base = symbol.split("/")[0]
    return base in _STABLES


def _is_non_crypto(symbol):
    base = symbol.split("/")[0]
    return base in _NON_CRYPTO


# ==============================
# MOMENTUM PAIR SELECTION
# ==============================
def momentum_score(symbol, market_type):
    """
    Score = ATR% * recent 1h volume, boosted when volume is surging NOW.
    - vol_avg_usdt: last 3 candles (3h) — captures current activity
    - surge_mult: if last 1h vol > 20-candle avg, coin is heating up NOW
    """
    # Require ~6 months of daily history — blocks newly listed and manipulated tokens
    # before they waste any further API calls or appear in the top pairs list.
    df_1d = get_cached_tf(symbol, "1d", market_type)
    if df_1d is None or len(df_1d) < 180:
        return 0

    df = get_cached_tf(symbol, "1h", market_type)
    if df is None or len(df) < 20:
        return 0

    close = df['close'].iloc[-1]
    if close <= 0:
        return 0

    vol_avg_usdt = df['volume'].tail(3).mean() * close
    if vol_avg_usdt < 75_000:  # $75K USDT per candle minimum
        return 0

    # Volume surge: reward coins breaking out in volume RIGHT NOW
    vol_ma20 = df['volume'].tail(20).mean()
    last_vol = df['volume'].iloc[-1]
    surge_mult = min(last_vol / vol_ma20, 3.0) if vol_ma20 > 0 else 1.0

    hl = df['high'] - df['low']
    hc = (df['high'] - df['close'].shift()).abs()
    lc = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]

    atr_pct = (atr / close) * 100
    return atr_pct * vol_avg_usdt * surge_mult


def _get_liquid_active_pool(exchange, market_type, symbol_filter, top_n=50):
    """
    One fetch_tickers() call → filter stablecoins → gate on liquidity
    and recent movement → sort by 24h volume → return top N.

    Gates (pre-filter before momentum scoring):
      - 24h quoteVolume > $2M   : real liquidity floor
      - |24h % change| > 1.5%  : coin is MOVING today, not dead

    Resilience:
      - On first timeout: wait 5s and retry once
      - On second failure: use cached tickers from last successful call
        (volume-ranked, far better than alphabetical market list)
      - Cache age logged so staleness is visible
      - Only falls back to raw market list if cache is also empty
    """
    global _tickers_cache, _tickers_cache_time

    tickers = None
    for attempt in range(2):
        try:
            tickers = exchange.fetch_tickers()
            _tickers_cache[market_type]      = tickers
            _tickers_cache_time[market_type] = time.time()
            break
        except Exception as e:
            if attempt == 0:
                print(f"⚠️ fetch_tickers error ({market_type}): {type(e).__name__}: {e} — retrying in 5s")
                time.sleep(5)
            else:
                cache_age_min = int((time.time() - _tickers_cache_time[market_type]) / 60)
                if _tickers_cache[market_type] is not None:
                    print(f"⚠️ fetch_tickers failed ({market_type}) — using cached tickers ({cache_age_min} min old)")
                    tickers = _tickers_cache[market_type]
                else:
                    print(f"⚠️ fetch_tickers failed ({market_type}), no cache — using market list fallback")
                    markets = SPOT_MARKETS if market_type == "spot" else FUTURES_MARKETS
                    return [s for s in markets
                            if symbol_filter(s) and not _is_stable(s) and not _is_non_crypto(s)][:top_n]

    pool = []
    for sym, t in tickers.items():
        if not symbol_filter(sym):
            continue
        if _is_stable(sym) or _is_non_crypto(sym):
            continue
        # MEXC futures often returns quoteVolume=None — fall back to
        # baseVolume * last price to get USDT-denominated volume
        vol_24h = t.get("quoteVolume") or 0
        if vol_24h == 0:
            last_price = t.get("last") or 0
            base_vol   = t.get("baseVolume") or 0
            vol_24h    = last_price * base_vol
        if vol_24h < 2_000_000:
            continue
        # percentage can be None on MEXC futures — skip movement gate
        # if data is unavailable rather than rejecting the whole pool
        pct_raw = t.get("percentage")
        if pct_raw is not None and abs(pct_raw) < 1.5:
            continue
        pool.append((sym, vol_24h))

    pool.sort(key=lambda x: x[1], reverse=True)
    result = [s for s, _ in pool[:top_n]]

    if not result:
        print(f"⚠️ fetch_tickers ({market_type}): pool empty after filters — using market list fallback")
        markets = SPOT_MARKETS if market_type == "spot" else FUTURES_MARKETS
        return [s for s in markets
                if symbol_filter(s) and not _is_stable(s) and not _is_non_crypto(s)][:top_n]

    return result


def get_pairs():
    """
    Pipeline:
      1. fetch_tickers() → liquid ($2M+) + active (1.5%+ move) MEXC futures pairs
      2. Score top 60 by ATR% × 1h_vol × surge_multiplier
      3. Return best 30 — these go into the strategy
    """
    candidates = []

    futures_syms = _get_liquid_active_pool(
        futures_exchange, "futures",
        lambda s: "/USDT:USDT" in s,
        top_n=60,
    )

    for symbol in futures_syms:
        score = momentum_score(symbol, "futures")
        if score > 0:
            candidates.append((symbol, "futures", score))

    candidates.sort(key=lambda x: x[2], reverse=True)
    top = [(sym, mtype) for sym, mtype, _ in candidates[:30]]
    print(f"📊 Top pairs: {[s for s, _ in top]}")
    return top


# ==============================
# ENTRY HIT CHECK
# ==============================
def entry_hit(df, entry, direction, trade_type):
    if df is None or df.empty or len(df) < 2:
        return False
    last = df.iloc[-1]
    prev = df.iloc[-2]

    if trade_type == "trend":
        if direction == "BUY":
            return last['low'] <= entry
        return last['high'] >= entry

    if trade_type == "reversal":
        if direction == "BUY":
            return (
                last['low'] <= entry
                and last['close'] > prev['high']
                and last['close'] > last['open']
            )
        return (
            last['high'] >= entry
            and last['close'] < prev['low']
            and last['close'] < last['open']
        )

    if trade_type == "bounce":
        # Entry at support — price dips to the level and shows a bullish close.
        # No need for prev_high break like reversal; the bounce candle already
        # confirmed the setup at signal time.
        if direction == "BUY":
            return last['low'] <= entry and last['close'] > last['open']

    return False


def is_not_late_entry(df, entry, direction, trade_type="trend"):
    if df is None or df.empty:
        return False
    price = df.iloc[-1]['close']
    # Trend breakouts retest the breakout level — allow wider tolerance.
    # Reversals and bounces must be entered at the turning point — keep tight.
    # If price has moved away from a support bounce, the edge is gone.
    threshold = 0.015 if trade_type == "trend" else 0.003
    return abs(price - entry) / entry <= threshold


# ==============================
# CHECK PENDING TRADES
# ==============================
def check_pending_trades():
    global pending_trades
    updated = []

    for trade in pending_trades:
        symbol = trade['pair']
        market_type = trade['market_type']

        expiry_hours = 3 if trade.get('trade_type') == 'bounce' else 1
        if datetime.now() - trade['time'] > timedelta(hours=expiry_hours):
            print(f"❌ Expired ({expiry_hours}h): {symbol}")
            continue

        key = f"{symbol}_15m_{market_type}"
        if key not in MARKET_DATA:
            updated.append(trade)
            continue

        df, _ = MARKET_DATA[key]
        if df is None or df.empty:
            updated.append(trade)
            continue

        if entry_hit(df, trade['entry'], trade['signal'], trade['trade_type']):
            if not is_not_late_entry(df, trade['entry'], trade['signal'], trade.get('trade_type', 'trend')):
                print(f"⚠️ Late entry skipped: {symbol}")
                continue

            print(f"✅ ENTRY HIT: {symbol}")
            direction = "LONG" if trade['signal'] == "BUY" else "SHORT"
            market_label = "Spot" if trade['market_type'] == "spot" else "Futures"
            tp2 = trade.get('tp2')
            tp2_line = f"TP2     {_fmt_price(tp2)}\n" if tp2 else ""
            send_telegram(
                f"✅ ENTRY TRIGGERED\n"
                f"{'─' * 22}\n"
                f"{symbol}  {direction}  [{market_label}]\n\n"
                f"Entry   {_fmt_price(trade['entry'])}\n"
                f"SL      {_fmt_price(trade['sl'])}\n"
                f"TP1     {_fmt_price(trade['tp'])}  ← close 50%\n"
                f"{tp2_line}"
                f"RR      1 : {trade['rr']}\n"
                f"{'─' * 22}\n"
                f"🔔 Trade is now live. Managing SL/TP."
            )
            _rd, _, _, _ = calc_position_size(trade['entry'], trade['sl'], trade.get('rr', 0.0))
            save_trade(
                trade['pair'], trade['signal'], trade['entry'],
                trade['sl'], trade['tp'], tp2, trade['rr'],
                trade['market_type'], trade.get('atr', 0.0), _rd
            )
        else:
            updated.append(trade)

    pending_trades = updated


# ==============================
# MAIN SCAN
# ==============================
def run_bot():
    global pending_trades

    prune_last_signals()

    if at_max_capacity():
        print(f"⚠️ Max concurrent trades ({MAX_CONCURRENT}) reached, skipping scan")
        check_pending_trades()
        return

    if _in_loss_cooldown():
        print("🧊 Loss cooldown active (15 min) — no new entries this scan")
        check_pending_trades()
        return

    print(f"\n🚀 Scan: {datetime.now()}\n")

    refresh_markets_if_needed()
    pairs = get_pairs()

    # ── Phase 1: fetch + indicators for all pairs ──────────────────────────
    # We need all pairs' indicator data before generating any signals so we
    # can compute bear_breadth and determine the macro market mode first.
    all_data    = {}   # symbol → (df_15m, df_1h, df_4h, df_1d, market_type)
    breadth_data = {}  # symbol → df_1h  (for bear_breadth computation)

    for symbol, market_type in pairs:
        df_15m, source = fetch_tf(symbol, "15m", market_type)
        df_1h = get_cached_tf(symbol, "1h", market_type)
        df_4h = get_cached_tf(symbol, "4h", market_type)
        df_1d = get_cached_tf(symbol, "1d", market_type)

        if any(x is None or x.empty for x in [df_15m, df_1h, df_4h, df_1d]):
            continue
        # Need enough history for EMA200 + ADX warmup
        if any(len(x) < 50 for x in [df_15m, df_1h, df_4h, df_1d]):
            continue

        df_15m = apply_indicators(df_15m)
        df_1h  = apply_indicators(df_1h)
        df_4h  = apply_indicators(df_4h)
        df_1d  = apply_indicators(df_1d)

        all_data[symbol]     = (df_15m, df_1h, df_4h, df_1d, market_type)
        breadth_data[symbol] = df_1h

    # Determine macro market mode from breadth before any signal evaluation
    _update_market_mode(breadth_data)

    # ── Phase 2: generate signals with market mode applied ─────────────────
    for symbol, (df_15m, df_1h, df_4h, df_1d, market_type) in all_data.items():
        # Per-coin snapshot so we can see what each pair is doing each scan
        try:
            _l1h = df_1h.iloc[-1]
            _l4h = df_4h.iloc[-1]
            _adx   = _l4h.get("adx") or 0
            _rsi   = _l1h.get("rsi") or 0
            _stoch = _l4h.get("stoch_k") or 0
            _e50   = _l1h.get("ema50") or 0
            _e200  = _l1h.get("ema200") or 0
            _ema_s = "bull" if _e50 > _e200 else "bear"
            print(f"  {symbol:<22} ADX4h={_adx:5.1f}  RSI1h={_rsi:5.1f}  stoch4h={_stoch:5.1f}  EMA={_ema_s}")
        except Exception:
            pass

        result = generate_pullback_signal(
            df_15m, df_1h, df_4h, df_1d,
            symbol=symbol, market_mode=_market_mode
        )
        if not result:
            continue

        sig, entry, sl, tp, tp2, rr, atr, trade_type = result

        # Skip if already pending
        if any(t['pair'] == symbol for t in pending_trades):
            print(f"⚠️ Already pending: {symbol}")
            continue

        # Skip if already active in DB
        try:
            engine = get_engine()
            active = pd.read_sql(
                f"SELECT pair FROM {TRADES_TABLE} WHERE status = 'OPEN' AND pair = %(pair)s",
                engine, params={"pair": symbol}
            )
            if not active.empty:
                print(f"⚠️ Already active: {symbol}")
                continue
        except Exception:
            pass

        if not is_new_signal(symbol, sig):
            # Allow re-entry if last closed trade today hit TP1 (continuation)
            if not _had_tp1_hit_today(symbol):
                continue
            print(f"♻️ Re-entry allowed (TP1 hit earlier today): {symbol}")

        if _directional_count(sig) >= 5:
            print(f"⚠️ {symbol} skipped — {sig} direction full (5/5)")
            continue

        sl_pct = abs(sl - entry) / entry * 100
        tp1_pct = abs(tp - entry) / entry * 100
        direction = "🟢 LONG" if sig == "BUY" else "🔴 SHORT"
        exchange = "KuCoin" if market_type == "spot" else "MEXC"
        market_label = f"{'Spot' if market_type == 'spot' else 'Futures'} · {exchange}"
        now_str = datetime.utcnow().strftime("%d %b %Y · %H:%M UTC")

        risk_dollars, units, pos_value, risk_pct = calc_position_size(entry, sl, rr)

        if pos_value > ACCOUNT_BALANCE * 10:
            print(f"⚠️ {symbol} skipped — position ${pos_value:.2f} > 10× account (${ACCOUNT_BALANCE:.2f})")
            continue

        # Contracts (MEXC uses contracts, not raw units)
        mkt_info     = FUTURES_MARKETS.get(symbol, {})
        contract_size = float(mkt_info.get('contractSize') or 1)
        contracts    = round(units / contract_size, 1)
        min_qty      = ((mkt_info.get('limits') or {}).get('amount') or {}).get('min') or 1
        if contracts < min_qty:
            print(f"⚠️ {symbol} skipped — {contracts} contracts below exchange minimum ({min_qty})")
            continue

        leverage = max(1, round(pos_value / ACCOUNT_BALANCE))
        leverage_line = f"Leverage  set {leverage}×  on MEXC before entering\n"

        # Funding rate + isolated margin reminder
        try:
            fr = futures_exchange.fetch_funding_rate(symbol)
            fr_val = (fr.get('fundingRate') or 0) * 100
            funding_line = f"Funding   {fr_val:+.4f}%/8h  ← use isolated margin\n"
        except Exception:
            funding_line = "Funding   n/a  ← use isolated margin\n"

        tp2_line = (
            f"TP2     {_fmt_price(tp2)}  (▲ {abs(tp2 - entry) / entry * 100:.2f}%)\n"
            if tp2 else ""
        )

        if trade_type == "bounce":
            # Bounce signals: candle confirmation already happened on this candle.
            # Price is AT support right now — enter at market immediately.
            # No pending queue; the setup doesn't improve by waiting.
            msg = (
                f"{'─' * 22}\n"
                f"{direction}  [{trade_type.upper()}]  🟡 MARKET ENTRY\n"
                f"{'─' * 22}\n"
                f"Pair    {symbol}\n"
                f"Market  {market_label}\n"
                f"Time    {now_str}\n\n"
                f"Entry   {_fmt_price(entry)}  ← enter NOW at market\n"
                f"SL      {_fmt_price(sl)}  (▼ {sl_pct:.2f}%)\n"
                f"TP1     {_fmt_price(tp)}  (▲ {tp1_pct:.2f}%)  ← close 50%\n"
                f"{tp2_line}"
                f"RR      1 : {rr}\n"
                f"{'─' * 22}\n"
                f"💰 POSITION SIZING  ({risk_pct*100:.0f}% risk)\n"
                f"Risk    ${risk_dollars:.2f}  of ${ACCOUNT_BALANCE:.2f}\n"
                f"Size    {contracts} contracts  (~${pos_value:.2f})\n"
                f"{leverage_line}"
                f"{funding_line}"
                f"{'─' * 22}\n"
                f"🔔 Trade is now live. Managing SL/TP."
            )
            print(msg)
            send_telegram(msg)
            save_trade(symbol, sig, entry, sl, tp, tp2, rr, market_type, float(atr), risk_dollars)
        else:
            msg = (
                f"{'─' * 22}\n"
                f"{direction}  [{trade_type.upper()}]\n"
                f"{'─' * 22}\n"
                f"Pair    {symbol}\n"
                f"Market  {market_label}\n"
                f"Time    {now_str}\n\n"
                f"Entry   {_fmt_price(entry)}\n"
                f"SL      {_fmt_price(sl)}  (▼ {sl_pct:.2f}%)\n"
                f"TP1     {_fmt_price(tp)}  (▲ {tp1_pct:.2f}%)  ← close 50%\n"
                f"{tp2_line}"
                f"RR      1 : {rr}\n"
                f"{'─' * 22}\n"
                f"💰 POSITION SIZING  ({risk_pct*100:.0f}% risk)\n"
                f"Risk    ${risk_dollars:.2f}  of ${ACCOUNT_BALANCE:.2f}\n"
                f"Size    {contracts} contracts  (~${pos_value:.2f})\n"
                f"{leverage_line}"
                f"{funding_line}"
                f"{'─' * 22}\n"
                f"⏳ Pending — waiting for entry to trigger"
            )
            print(msg)
            send_telegram(msg)

            pending_trades.append({
                "pair": symbol,
                "signal": sig,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "tp2": tp2,
                "rr": rr,
                "market_type": market_type,
                "trade_type": trade_type,
                "atr": float(atr),
                "time": datetime.now()
            })

    check_pending_trades()


# ==============================
# MAIN LOOP
# ==============================
def main():
    global pending_trades, PRICE_CACHE, MARKET_DATA

    ensure_csv()
    pending_trades = load_pending_trades()
    _restore_last_signals()
    print(f"📂 Loaded {len(pending_trades)} pending trade(s) from DB")

    last_report_day = None

    while True:
        try:
            MARKET_DATA = {}
            PRICE_CACHE = {}

            global ACCOUNT_BALANCE
            ACCOUNT_BALANCE = get_compounded_balance(STARTING_BALANCE)
            print(f"💼 Balance: ${ACCOUNT_BALANCE:.2f}  (started ${STARTING_BALANCE:.2f})")

            check_telegram_commands()

            if daily_loss_limit_hit():
                print(f"🛑 Daily loss limit ({MAX_DAILY_LOSSES}) hit — skipping new signals")
            else:
                run_bot()

            check_trade_results(get_price, send_telegram)

            today = datetime.now().date()
            if last_report_day != today:
                daily_report(send_telegram)
                send_csv(TOKEN, CHAT_ID)
                last_report_day = today

            save_pending_trades(pending_trades)

        except Exception as e:
            print(f"⚠️ Loop error: {e}")
            send_telegram(f"⚠️ Bot error (will retry in 60s):\n{e}")
            time.sleep(60)
            continue

        hour = datetime.utcnow().hour
        in_session = (7 <= hour < 9) or (13 <= hour < 15)
        sleep_secs = 300 if in_session else 900
        print(f"⏳ Sleeping {sleep_secs // 60} minutes{'  (session active)' if in_session else ''}...")
        time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
