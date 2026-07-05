"""
A2 Strategy — HTF zone -> 5min CHoCH trigger (CAUSAL, live-parity verified)
===========================================================================
⛔ STATUS: NOT VALIDATED — config must stay `enabled: False` in app.py.
   Causal re-validation (2026-07-04) showed the original A2 edge was a
   swing-confirmation look-ahead artifact (2025 PF 1.65 -> 0.90 causal).
   This module is retained because it is the PARITY-PROVEN reference
   implementation (166/166 trade reproduction vs arch2c_v2.py) and the
   template for any future zone->trigger strategy.

Projection rule: an LTF bar may only use zone state from the last HTF bar
that FULLY ENDED before the LTF bar opened. Swing levels usable only from
their confirmation bar (extreme + SWING bars).
"""

from datetime import time as dtime

import numpy as np
import pandas as pd

import sys
from pathlib import Path
_P8 = Path(__file__).resolve().parent.parent
if str(_P8) not in sys.path:
    sys.path.insert(0, str(_P8))
import smc_backtest as bt

from signal_engine import Signal

# ── Parameters (must match arch2c_v2.py — the causal live-equivalent backtest)
SWING          = 10      # swing confirmation lag (bars)
OB_LOOKBACK    = 20
ZONE_LIFETIME  = 50      # FVG zone lifetime (HTF bars)
OB_ZONE_LIFE   = 40      # OB zone lifetime cap (HTF bars)
TRIGGER_WINDOW = 30      # LTF bars between zone tag and trigger
RR_TARGET      = 2.0
LTF_TF         = 5
ENTRY_CUTOFF   = dtime(14, 5)


def _drop_incomplete(df, tf_minutes, last_1min_ts):
    if df.empty:
        return df
    last_row_complete = (df["ts"].iloc[-1] + pd.Timedelta(minutes=tf_minutes)
                         <= last_1min_ts + pd.Timedelta(minutes=1))
    return df if last_row_complete else df.iloc[:-1].reset_index(drop=True)


def _causal_levels(df):
    sh = df["high"].where(df["is_swing_high"]).shift(SWING).ffill()
    sl = df["low"].where(df["is_swing_low"]).shift(SWING).ffill()
    return sh, sl


def build_causal_ob_zones(h: pd.DataFrame):
    n = len(h)
    o = h["open"].to_numpy(dtype=float)
    c = h["close"].to_numpy(dtype=float)
    hi = h["high"].to_numpy(dtype=float)
    lo = h["low"].to_numpy(dtype=float)

    sh, sl = _causal_levels(h)
    last_sh = sh.to_numpy(dtype=float)
    last_sl = sl.to_numpy(dtype=float)
    bull_bos = (c > last_sh) & ~np.isnan(last_sh)
    bear_bos = (c < last_sl) & ~np.isnan(last_sl)

    idx = np.arange(n, dtype=float)
    bearish_idx = pd.Series(np.where(c < o, idx, np.nan)).ffill().to_numpy()
    bullish_idx = pd.Series(np.where(c > o, idx, np.nan)).ffill().to_numpy()

    def _events(bos_mask, opp_idx):
        ev = []
        for b in np.where(bos_mask)[0]:
            j_f = opp_idx[b - 1] if b >= 1 else np.nan
            if np.isnan(j_f) or j_f < b - OB_LOOKBACK:
                continue
            j = int(j_f)
            ev.append((b + 1, max(o[j], c[j]), min(o[j], c[j])))
        return ev

    def _fill(events, inval_arr, is_bull):
        top_a = np.full(n, np.nan); bot_a = np.full(n, np.nan)
        for a, top, bot in events:
            if a >= n:
                continue
            seg = inval_arr[a:]
            inv = np.where(seg < bot)[0] if is_bull else np.where(seg > top)[0]
            e = a + int(inv[0]) if len(inv) else n
            e = min(e, a + OB_ZONE_LIFE)
            top_a[a:e] = top; bot_a[a:e] = bot
        return top_a, bot_a

    b_top, b_bot = _fill(_events(bull_bos, bearish_idx), lo, True)
    r_top, r_bot = _fill(_events(bear_bos, bullish_idx), hi, False)
    return b_top, b_bot, r_top, r_bot


def build_fvg_zones(h: pd.DataFrame):
    n = len(h)
    close = h["close"].to_numpy(dtype=float)
    ftop  = h["fvg_top"].to_numpy(dtype=float)
    fbot  = h["fvg_bottom"].to_numpy(dtype=float)

    bull_top = np.full(n, np.nan); bull_bot = np.full(n, np.nan)
    bear_top = np.full(n, np.nan); bear_bot = np.full(n, np.nan)

    def _place(events, inval_cond, top_arr, bot_arr):
        for i in events:
            top, bot = ftop[i], fbot[i]
            s = i + 2
            if s >= n:
                continue
            e = min(s + ZONE_LIFETIME, n)
            inv = np.where(inval_cond(close[s:e], top, bot))[0]
            end = s + (int(inv[0]) if len(inv) else ZONE_LIFETIME)
            top_arr[s:min(end, n)] = top
            bot_arr[s:min(end, n)] = bot

    _place(np.where(h["fvg_bull"].to_numpy())[0], lambda c, t, b: c < b, bull_top, bull_bot)
    _place(np.where(h["fvg_bear"].to_numpy())[0], lambda c, t, b: c > t, bear_top, bear_bot)
    return bull_top, bull_bot, bear_top, bear_bot


def scan_symbol_a2(symbol: str, df_1min: pd.DataFrame, strategy_cfg: dict,
                   session_windows=None) -> "list[Signal]":
    signals: list[Signal] = []
    try:
        zone_type = strategy_cfg.get("zone_type", "ob")
        htf_tf    = int(strategy_cfg.get("htf_tf", 60))
        last_ts   = pd.Timestamp(df_1min["ts"].iloc[-1])

        h = bt.resample_ohlcv(df_1min, htf_tf)      # end-time projection handles tail
        if len(h) < 25:
            return signals
        h = bt.detect_swings(h)
        if zone_type == "ob":
            zb_top, zb_bot, zr_top, zr_bot = build_causal_ob_zones(h)
        else:
            h = bt.detect_fvgs(h)
            zb_top, zb_bot, zr_top, zr_bot = build_fvg_zones(h)
        hts_end = h["ts"].to_numpy() + np.timedelta64(htf_tf, "m")

        d = bt.resample_ohlcv(df_1min, LTF_TF)
        d = _drop_incomplete(d, LTF_TF, last_ts)
        if len(d) < 30:
            return signals
        d = bt.detect_swings(d)
        close = d["close"]
        last_sh, last_sl = _causal_levels(d)
        trig_up   = ((close > last_sh) & (close.shift(1) <= last_sh) & last_sh.notna()).to_numpy()
        trig_down = ((close < last_sl) & (close.shift(1) >= last_sl) & last_sl.notna()).to_numpy()

        lts = d["ts"].to_numpy()
        pos = np.searchsorted(hts_end, lts, side="right") - 1
        valid = pos >= 0

        def _proj(arr):
            v = np.full(len(lts), np.nan)
            v[valid] = arr[pos[valid]]
            return v

        pzb_top, pzb_bot = _proj(zb_top), _proj(zb_bot)
        pzr_top, pzr_bot = _proj(zr_top), _proj(zr_bot)

        low_arr   = d["low"].to_numpy(dtype=float)
        high_arr  = d["high"].to_numpy(dtype=float)
        close_arr = close.to_numpy(dtype=float)

        bull_tag = np.where(~np.isnan(pzb_top) & (low_arr <= pzb_top) & (close_arr >= pzb_bot))[0]
        bear_tag = np.where(~np.isnan(pzr_top) & (high_arr >= pzr_bot) & (close_arr <= pzr_top))[0]

        n = len(d)
        for t in range(max(0, n - 2), n):
            ts = pd.Timestamp(d["ts"].iloc[t])
            if ts.time() > ENTRY_CUTOFF or ts.time() < dtime(9, 15):
                continue
            for direction in ("bull", "bear"):
                if direction == "bull":
                    if not trig_up[t] or np.isnan(pzb_top[t]):
                        continue
                    zone_top, zone_bot = float(pzb_top[t]), float(pzb_bot[t])
                    j = int(np.searchsorted(bull_tag, t + 1)) - 1
                    if j < 0 or bull_tag[j] < t - TRIGGER_WINDOW:
                        continue
                    entry = float(close_arr[t])
                    if entry < zone_bot:
                        continue
                    sl = zone_bot; risk = entry - sl
                    if risk <= 0:
                        continue
                    tp = entry + RR_TARGET * risk
                else:
                    if not trig_down[t] or np.isnan(pzr_top[t]):
                        continue
                    zone_top, zone_bot = float(pzr_top[t]), float(pzr_bot[t])
                    j = int(np.searchsorted(bear_tag, t + 1)) - 1
                    if j < 0 or bear_tag[j] < t - TRIGGER_WINDOW:
                        continue
                    entry = float(close_arr[t])
                    if entry > zone_top:
                        continue
                    sl = zone_top; risk = sl - entry
                    if risk <= 0:
                        continue
                    tp = entry - RR_TARGET * risk

                signals.append(Signal(
                    timestamp=ts.to_pydatetime(),
                    symbol=symbol,
                    strategy=strategy_cfg.get("name", "A2"),
                    direction=direction,
                    entry_tf=LTF_TF,
                    entry_price=round(entry, 2),
                    sl_price=round(sl, 2),
                    tp_price=round(tp, 2),
                    fvg_top=round(zone_top, 2),
                    fvg_bottom=round(zone_bot, 2),
                    pd_zone=f"{zone_type}_zone",
                    htf_signal=f"{zone_type}_causal",
                    htf_tf=htf_tf,
                    risk_pts=round(risk, 2),
                ))
    except Exception:
        return signals
    return signals
