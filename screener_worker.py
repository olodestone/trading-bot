#!/usr/bin/env python3
"""
Isolated scan subprocess.

Spawned by bot.py each cycle. Imports numpy/pandas/strategy ONLY — ccxt is
gone. OHLCV and ticker data are fetched via direct REST calls (requests), which
is already resident in the pandas/numpy stack and adds zero marginal RSS.

ccxt cost: 58 MB. Removing it is the single biggest per-scan RSS saving.

Scan speed also improves: ccxt's rateLimit=1200 forced 1.2s between every call
(120 calls × 1.2s = ~144s). Direct HTTP with a 0.1s delay = ~12s + latency.

RSS is measured at three checkpoints:
  rss_before_imports_kb  — process baseline
  rss_after_imports_kb   — after numpy + pandas + strategy (NO ccxt)
  rss_after_scan_kb      — after full scan + explicit cleanup
"""
import gc, json, os, sys, time
import signal as _signal
from datetime import datetime


# ──────────────────────────────────────────────────────────────────────────────
# RSS HELPER
# ──────────────────────────────────────────────────────────────────────────────
def _rss_kb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        return 0


_rss_t0 = _rss_kb()
print(f"[worker] RSS before imports: {_rss_t0:,} kB", flush=True)


# ──────────────────────────────────────────────────────────────────────────────
# HEAVY IMPORTS  (NO ccxt — requests is already resident via numpy/pandas stack)
# ──────────────────────────────────────────────────────────────────────────────
import requests
import numpy as np
import pandas as pd
from strategy import (apply_indicators, generate_filtered_signal,
                      generate_pullback_signal, compute_confidence)

_rss_t1 = _rss_kb()
print(f"[worker] RSS after imports:  {_rss_t1:,} kB  (+{_rss_t1 - _rss_t0:,} kB)", flush=True)


# ──────────────────────────────────────────────────────────────────────────────
# NUMPY JSON ENCODER
# ──────────────────────────────────────────────────────────────────────────────
class _NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):  return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.ndarray):  return obj.tolist()
        return super().default(obj)


# ──────────────────────────────────────────────────────────────────────────────
# BLOCKLISTS
# ──────────────────────────────────────────────────────────────────────────────
_STABLES = {
    "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "FRAX", "USDP",
    "UST", "USDD", "SUSD", "GUSD", "LUSD", "PYUSD", "USDJ",
    "CUSD", "CEUR", "EURS", "ALUSD", "USDN", "MUSD", "USDX",
}
_NON_CRYPTO = {
    "XAUT", "PAXG", "CACHE", "XAU", "XAG", "XAGT",
    "SILVER", "GOLD", "USOIL", "UKOIL", "OIL", "BRENT", "WTI",
    "WHEAT", "CORN", "SOYB",
}

def _is_stable(sym):     return sym.split("/")[0] in _STABLES
def _is_non_crypto(sym): return sym.split("/")[0] in _NON_CRYPTO


# ──────────────────────────────────────────────────────────────────────────────
# RATE LIMITER  (shared across all HTTP fetch calls)
# ──────────────────────────────────────────────────────────────────────────────
_last_kucoin = 0.0
_last_mexc   = 0.0
_KUCOIN_GAP  = 0.12   # 8 req/s  (limit: 30 req/3s public)
_MEXC_GAP    = 0.12   # 8 req/s


def _wait_kucoin():
    global _last_kucoin
    gap = _KUCOIN_GAP - (time.time() - _last_kucoin)
    if gap > 0:
        time.sleep(gap)
    _last_kucoin = time.time()


def _wait_mexc():
    global _last_mexc
    gap = _MEXC_GAP - (time.time() - _last_mexc)
    if gap > 0:
        time.sleep(gap)
    _last_mexc = time.time()


# ──────────────────────────────────────────────────────────────────────────────
# KUCOIN SPOT  (direct HTTP — no ccxt)
# ──────────────────────────────────────────────────────────────────────────────
_KC_TF = {"15m": "15min", "1h": "1hour", "4h": "4hour", "1d": "1day"}
_KC_TF_SECS = {"15min": 900, "1hour": 3600, "4hour": 14400, "1day": 86400}

_SESS = requests.Session()   # reuse TCP connections across calls
_SESS.headers.update({"Accept": "application/json"})


def _kucoin_tickers():
    """Returns {sym: {quoteVolume, last, percentage, baseVolume}}."""
    _wait_kucoin()
    r = _SESS.get("https://api.kucoin.com/api/v1/market/allTickers", timeout=12)
    out = {}
    for t in (r.json().get("data") or {}).get("ticker") or []:
        raw = t.get("symbol", "")
        sym = raw.replace("-", "/")         # "BTC-USDT" → "BTC/USDT"
        try:
            change = t.get("changeRate")
            out[sym] = {
                "quoteVolume": float(t.get("volValue") or 0),
                "last":        float(t.get("last") or 0),
                "percentage":  float(change) * 100 if change is not None else None,
                "baseVolume":  float(t.get("vol") or 0),
            }
        except (TypeError, ValueError):
            pass
    return out


def _kucoin_ohlcv(symbol, tf, limit=200):
    """Fetch OHLCV for a KuCoin spot pair. Returns list of [ts_ms, o, h, l, c, v]."""
    sym_dash = symbol.replace("/", "-").split(":")[0]   # "BTC/USDT" → "BTC-USDT"
    tf_str   = _KC_TF.get(tf, "1hour")
    tf_secs  = _KC_TF_SECS.get(tf_str, 3600)

    end   = int(time.time())
    start = end - (limit + 5) * tf_secs

    for attempt in range(2):
        try:
            _wait_kucoin()
            r = _SESS.get(
                "https://api.kucoin.com/api/v1/market/candles",
                params={"symbol": sym_dash, "type": tf_str,
                        "startAt": start, "endAt": end},
                timeout=12,
            )
            data = r.json().get("data") or []
            # KuCoin returns newest-first: [time_s, open, close, high, low, vol, turnover]
            rows = [
                [int(c[0]) * 1000,   # s → ms
                 float(c[1]),        # open
                 float(c[3]),        # high  (index 3 in KuCoin)
                 float(c[4]),        # low   (index 4 in KuCoin)
                 float(c[2]),        # close (index 2 in KuCoin)
                 float(c[5])]        # volume
                for c in reversed(data)
            ]
            return rows[-limit:]
        except Exception as e:
            if attempt == 0:
                print(f"  KuCoin OHLCV retry {symbol} {tf}: {e}", flush=True)
                time.sleep(2)
    return []


# ──────────────────────────────────────────────────────────────────────────────
# MEXC FUTURES  (direct HTTP — no ccxt)
# ──────────────────────────────────────────────────────────────────────────────
_MX_TF = {"15m": "Min15", "1h": "Min60", "4h": "Hour4", "1d": "Day1"}


def _mexc_futures_tickers():
    """Returns {sym: {quoteVolume, last, percentage, baseVolume}}."""
    _wait_mexc()
    r = _SESS.get("https://contract.mexc.com/api/v1/contract/ticker", timeout=12)
    out = {}
    for t in r.json().get("data") or []:
        raw  = t.get("symbol", "")          # "BTC_USDT"
        base = raw.replace("_USDT", "")
        sym  = f"{base}/USDT:USDT"
        try:
            last = float(t.get("lastPrice") or 0)
            vol  = float(t.get("volume24") or 0)
            pct  = t.get("riseFallRate")
            out[sym] = {
                "quoteVolume": last * vol,
                "last":        last,
                "percentage":  float(pct) * 100 if pct is not None else None,
                "baseVolume":  vol,
            }
        except (TypeError, ValueError):
            pass
    return out


def _mexc_futures_ohlcv(symbol, tf, limit=200):
    """Fetch OHLCV for a MEXC futures pair. Returns list of [ts_ms, o, h, l, c, v]."""
    sym_mx = symbol.replace("/USDT:USDT", "_USDT").replace("/", "_")
    tf_str = _MX_TF.get(tf, "Min60")

    for attempt in range(2):
        try:
            _wait_mexc()
            r = _SESS.get(
                f"https://contract.mexc.com/api/v1/contract/kline/{sym_mx}",
                params={"interval": tf_str},
                timeout=12,
            )
            d     = r.json().get("data") or {}
            times = d.get("time", [])
            opens = d.get("open", [])
            highs = d.get("high", [])
            lows  = d.get("low", [])
            cls   = d.get("close", [])
            vols  = d.get("vol", [])
            rows = [
                [int(times[i]) * 1000,
                 float(opens[i]), float(highs[i]),
                 float(lows[i]),  float(cls[i]),
                 float(vols[i])]
                for i in range(len(times))
            ]
            return rows[-limit:]
        except Exception as e:
            if attempt == 0:
                print(f"  MEXC OHLCV retry {symbol} {tf}: {e}", flush=True)
                time.sleep(2)
    return []


# ──────────────────────────────────────────────────────────────────────────────
# UNIFIED OHLCV FETCH
# ──────────────────────────────────────────────────────────────────────────────
_RUN_CACHE: dict = {}   # dedup within a single run: "symbol_tf_mtype" → df


def _timeout_handler(signum, frame):
    raise TimeoutError("fetch timeout")


def fetch_tf(symbol, tf, market_type):
    key = f"{symbol}_{tf}_{market_type}"
    if key in _RUN_CACHE:
        return _RUN_CACHE[key]

    for attempt in range(2):
        try:
            _signal.signal(_signal.SIGALRM, _timeout_handler)
            _signal.alarm(15)
            if market_type == "spot":
                raw = _kucoin_ohlcv(symbol, tf)
            else:
                raw = _mexc_futures_ohlcv(symbol, tf)
            _signal.alarm(0)

            if not raw:
                return None

            df = pd.DataFrame(raw, columns=["time","open","high","low","close","volume"])
            _RUN_CACHE[key] = df
            return df
        except Exception as e:
            _signal.alarm(0)
            print(f"  fetch_tf retry {attempt+1} {symbol} {tf}: {e}", flush=True)
            time.sleep(2)

    return None


# ──────────────────────────────────────────────────────────────────────────────
# PAIR SELECTION
# ──────────────────────────────────────────────────────────────────────────────
def momentum_score(symbol, market_type):
    df_1d = fetch_tf(symbol, "1d", market_type)
    if df_1d is None or len(df_1d) < 180:
        return 0
    df = fetch_tf(symbol, "1h", market_type)
    if df is None or len(df) < 20:
        return 0
    close = df["close"].iloc[-1]
    if close <= 0:
        return 0
    vol_avg_usdt = df["volume"].tail(3).mean() * close
    if vol_avg_usdt < 75_000:
        return 0
    vol_ma20     = df["volume"].tail(20).mean()
    last_vol     = df["volume"].iloc[-1]
    surge_mult   = min(last_vol / vol_ma20, 3.0) if vol_ma20 > 0 else 1.0
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    atr_pct = (atr / close) * 100
    return atr_pct * vol_avg_usdt * surge_mult


def _get_liquid_active_pool(fetch_tickers_fn, symbol_filter, top_n=50):
    tickers = None
    for attempt in range(2):
        try:
            tickers = fetch_tickers_fn()
            break
        except Exception as e:
            if attempt == 0:
                print(f"  fetch_tickers retry: {e}", flush=True)
                time.sleep(5)
            else:
                print(f"  fetch_tickers failed both attempts", flush=True)

    if not tickers:
        return []

    pool = []
    for sym, t in tickers.items():
        if not symbol_filter(sym):   continue
        if _is_stable(sym):          continue
        if _is_non_crypto(sym):      continue
        vol_24h = t.get("quoteVolume") or 0
        if vol_24h == 0:
            vol_24h = (t.get("last") or 0) * (t.get("baseVolume") or 0)
        if vol_24h < 2_000_000:      continue
        pct_raw = t.get("percentage")
        if pct_raw is not None and abs(pct_raw) < 1.5:
            continue
        pool.append((sym, vol_24h))

    del tickers
    gc.collect()

    pool.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in pool[:top_n]]


def get_pairs():
    candidates = []

    spot_syms = _get_liquid_active_pool(
        _kucoin_tickers,
        lambda s: "/USDT" in s and ":USDT" not in s,
        50,
    )
    fut_syms = _get_liquid_active_pool(
        _mexc_futures_tickers,
        lambda s: "/USDT:USDT" in s,
        50,
    )

    for sym in spot_syms:
        score = momentum_score(sym, "spot")
        if score > 0:
            candidates.append((sym, "spot", score))
    for sym in fut_syms:
        score = momentum_score(sym, "futures")
        if score > 0:
            candidates.append((sym, "futures", score))

    candidates.sort(key=lambda x: x[2], reverse=True)
    top = [(s, m) for s, m, _ in candidates[:30]]
    print(f"Top pairs ({len(top)}): {[s for s, _ in top]}", flush=True)
    return top


# ──────────────────────────────────────────────────────────────────────────────
# MARKET MODE  (no DataFrames — pure Python EMA on raw closes)
# ──────────────────────────────────────────────────────────────────────────────
def _ema_py(closes, period):
    """EMA computed on a plain Python list — no pandas, no DataFrame allocation."""
    if not closes:
        return 0.0
    k   = 2.0 / (period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    return ema


def _breadth_for_pair(symbol, market_type):
    """
    Returns True (bearish) / False (bullish) / None (unavailable).
    Fetches raw 1h OHLCV and computes EMA50/EMA200 in pure Python.
    No DataFrame is created — single list of close prices only.
    """
    key = f"{symbol}_1h_{market_type}_raw"
    if market_type == "spot":
        raw = _kucoin_ohlcv(symbol, "1h", limit=201)
    else:
        raw = _mexc_futures_ohlcv(symbol, "1h", limit=201)

    if len(raw) < 201:
        return None

    closes = [c[4] for c in raw]   # index 4 = close in [ts, o, h, l, c, v]
    del raw
    ema50  = _ema_py(closes, 50)
    ema200 = _ema_py(closes, 200)
    del closes
    return ema50 < ema200


def _compute_market_mode(bear_count, total, prev_bear_scans=0,
                         prev_recovery_scans=0, prev_mode="normal"):
    if total == 0:
        return prev_mode, 0.0, 0, prev_bear_scans, prev_recovery_scans

    breadth = bear_count / total
    bear_scans     = prev_bear_scans
    recovery_scans = prev_recovery_scans

    if breadth > 0.65:
        bear_scans += 1;     recovery_scans = 0
    elif breadth <= 0.45:
        recovery_scans += 1; bear_scans = 0
    else:
        bear_scans     = max(0, bear_scans - 1)
        recovery_scans = max(0, recovery_scans - 1)

    mode = ("bear" if bear_scans >= 2
            else "recovery" if recovery_scans >= 3
            else "normal")

    print(f"Market mode: {mode.upper()} | breadth {breadth:.0%} "
          f"({bear_count}/{total} ema50<ema200)", flush=True)
    return mode, breadth, bear_count, bear_scans, recovery_scans


def _check_btc_macro():
    """Pure-Python EMA on raw closes — no apply_indicators, no DataFrame."""
    try:
        raw = _mexc_futures_ohlcv("BTC/USDT:USDT", "4h", limit=201)
        if len(raw) < 201:
            return {"downtrend": None, "ema50": None, "ema200": None}
        closes = [c[4] for c in raw]
        del raw
        ema50  = _ema_py(closes, 50)
        ema200 = _ema_py(closes, 200)
        del closes
        sep_pct = abs(ema50 - ema200) / ema200 * 100 if ema200 else 0
        tag = "bearish" if ema50 < ema200 else "bullish"
        print(f"BTC 4h: EMA50={ema50:.1f} EMA200={ema200:.1f} "
              f"({tag} sep={sep_pct:.2f}%)", flush=True)
        return {"downtrend": ema50 < ema200, "ema50": ema50, "ema200": ema200}
    except Exception as e:
        print(f"BTC macro error: {e}", flush=True)
        return {"downtrend": None, "ema50": None, "ema200": None}


# ──────────────────────────────────────────────────────────────────────────────
# MAIN SCAN
# ──────────────────────────────────────────────────────────────────────────────
def main(output_path):
    rss_pre_scan = _rss_kb()

    prev_mode           = os.getenv("PREV_MARKET_MODE",    "normal")
    prev_bear_scans     = int(os.getenv("PREV_BEAR_SCANS",    "0"))
    prev_recovery_scans = int(os.getenv("PREV_RECOVERY_SCANS", "0"))
    prev_btc_down       = os.getenv("PREV_BTC_DOWNTREND", "false").lower() == "true"

    hour_utc         = datetime.utcnow().hour
    session_filtered = 8 <= hour_utc <= 17

    # ── Pair selection ────────────────────────────────────────────────────────
    pairs = get_pairs()

    # ── Stage A: breadth pass (pure Python EMA — no DataFrames) ──────────────
    # Processes one pair at a time; only 2 floats retained per pair.
    bear_count     = 0
    breadth_total  = 0
    for symbol, market_type in pairs:
        result = _breadth_for_pair(symbol, market_type)
        if result is not None:
            breadth_total += 1
            if result:
                bear_count += 1
        if breadth_total % 10 == 0 and breadth_total > 0:
            gc.collect()

    gc.collect()

    # BTC macro — also pure Python, no DataFrame
    btc_result    = _check_btc_macro()
    btc_downtrend = (btc_result["downtrend"] if btc_result["downtrend"] is not None
                     else prev_btc_down)

    market_mode, bear_breadth, bear_count, new_bear_scans, new_recovery_scans = (
        _compute_market_mode(bear_count, breadth_total,
                             prev_bear_scans, prev_recovery_scans, prev_mode)
    )

    # ── Stage B: signal pass — ONE pair at a time (no accumulation) ──────────
    signals = []

    for i, (symbol, market_type) in enumerate(pairs):
        df_15m = df_1h = df_4h = df_1d = None
        try:
            df_15m = fetch_tf(symbol, "15m", market_type)
            df_1h  = fetch_tf(symbol, "1h",  market_type)
            df_4h  = fetch_tf(symbol, "4h",  market_type)
            df_1d  = fetch_tf(symbol, "1d",  market_type)

            if any(x is None or x.empty for x in [df_15m, df_1h, df_4h, df_1d]):
                continue
            if any(len(x) < 50 for x in [df_15m, df_1h, df_4h, df_1d]):
                continue

            df_15m = apply_indicators(df_15m)
            df_1h  = apply_indicators(df_1h)
            df_4h  = apply_indicators(df_4h)
            df_1d  = apply_indicators(df_1d)

            # Diagnostic log
            try:
                _l1h = df_1h.iloc[-1]; _l4h = df_4h.iloc[-1]
                print(f"  {symbol:<22} ADX4h={_l4h.get('adx',0):5.1f}  "
                      f"RSI1h={_l1h.get('rsi',0):5.1f}  "
                      f"EMA={'bull' if _l1h.get('ema50',0)>_l1h.get('ema200',1) else 'bear'}",
                      flush=True)
            except Exception:
                pass

            result = generate_filtered_signal(
                df_15m, df_1h, df_4h, df_1d,
                symbol=symbol, market_mode=market_mode, btc_downtrend=btc_downtrend
            )
            if not result:
                result = generate_pullback_signal(
                    df_15m, df_1h, df_4h, df_1d,
                    symbol=symbol, market_mode=market_mode
                )
            if not result:
                continue

            sig, entry, sl, tp, tp2, rr, atr, trade_type = result

            # For trend entries: record the breakout/breakdown level.
            # Pending entry check uses this to reject entries where price
            # has already fallen below the breakout (failed breakout).
            entry_valid_above = None
            entry_valid_below = None
            if trade_type == "trend":
                prev_candle = df_15m.iloc[-2]
                if sig == "BUY":
                    entry_valid_above = float(prev_candle["high"])
                else:
                    entry_valid_below = float(prev_candle["low"])

            conf = compute_confidence(
                sig, entry, sl, tp, tp2, rr, atr, trade_type,
                df_15m, df_1h, df_4h, df_1d, market_mode, btc_downtrend
            )

            if conf < 55:
                print(f"  {symbol}: skipped — low conf ({conf} < 55)", flush=True)
                continue
            if session_filtered and conf < 65:
                print(f"  {symbol}: skipped — off-peak conf ({conf} < 65)", flush=True)
                continue

            # Market info defaults — no load_markets() needed
            if market_type == "spot":
                mkt_info = {"min_qty": 0.001}
            else:
                mkt_info = {"min_qty": 1, "contract_size": 1}

            signals.append({
                "pair":              symbol,
                "market_type":       market_type,
                "signal":            sig,
                "entry":             float(entry),
                "sl":                float(sl),
                "tp":                float(tp),
                "tp2":               float(tp2) if tp2 is not None else None,
                "rr":                float(rr),
                "atr":               float(atr),
                "trade_type":        trade_type,
                "confidence":        int(conf),
                "market_info":       mkt_info,
                "entry_valid_above": entry_valid_above,
                "entry_valid_below": entry_valid_below,
            })
            print(f"  SIGNAL: {symbol} {sig} [{trade_type}] conf={conf}", flush=True)

        except Exception as e:
            print(f"  Error {symbol}: {e}", flush=True)
        finally:
            del df_15m, df_1h, df_4h, df_1d

        if (i + 1) % 10 == 0:
            gc.collect()

    # Clear within-run cache and collect
    _RUN_CACHE.clear()
    gc.collect()

    rss_post_scan = _rss_kb()
    print(f"[worker] RSS after scan:     {rss_post_scan:,} kB  "
          f"(delta from pre-scan: {rss_post_scan - rss_pre_scan:+,} kB)", flush=True)

    output = {
        "market_mode":           market_mode,
        "bear_breadth":          bear_breadth,
        "bear_count":            bear_count,
        "total_pairs":           breadth_total,
        "new_bear_scans":        new_bear_scans,
        "new_recovery_scans":    new_recovery_scans,
        "btc_downtrend":         btc_downtrend,
        "btc_ema50":             btc_result.get("ema50"),
        "btc_ema200":            btc_result.get("ema200"),
        "rss_before_imports_kb": _rss_t0,
        "rss_after_imports_kb":  _rss_t1,
        "rss_after_scan_kb":     rss_post_scan,
        "signals":               signals,
    }

    with open(output_path, "w") as f:
        json.dump(output, f, cls=_NumpyEncoder)

    print(f"[worker] Done. {len(signals)} signal(s) → {output_path}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: screener_worker.py <output_json_path>", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
