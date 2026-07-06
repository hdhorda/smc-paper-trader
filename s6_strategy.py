"""
s6_strategy.py — S6 live signal scanner
========================================
Strategy: Liquidity Sweep → HTF Delivery → CISD → IFVG entry

Signal chain (bullish):
  1. HTF (60min): last S6_HTF_N closed bars all bullish delivery (close > open)
  2. LTF: Liquidity Sweep of recent swing low (price dips below SSL, snaps back above)
  3. LTF: CISD — close crosses above the open of most recent bearish candle pre-sweep
  4. LTF: Entry when price retests active bullish IFVG zone (low ≤ ifvg_top, close ≥ ifvg_bot)

Bearish is exact mirror.

CAUSAL NOTE (2026-07-06):
  Backtest PF 2.03 (HTF60 + Entry3min, 1016 trades) is inflated — detect_swings()
  uses a centered window so backtested sweeps/CISD fire ~10 bars early. In live,
  the last 10 bars have NaN swing status (no future bars), so sweeps can only
  detect from confirmed swing levels. Live is naturally causal.
  Paper trading is the true OOS test. Expect fewer signals than backtest implied.

Dispatch: app.py calls scan_symbol_s6() when strategy config has engine="s6".
"""

import sys
import bisect
import logging
import warnings
from pathlib import Path
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# Load smc_backtest from Project_8 (unconditional insert — ensures parent copy
# is found before live_trading/smc_backtest.py, which lacks S6 functions).
_P8 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_P8))
import smc_backtest as bt

from signal_engine import Signal, _in_session

# How many LTF bars back to scan for a CISD trigger
CISD_SCAN_WINDOW = 15


def _prep_s6(df_1min: pd.DataFrame, tf: int) -> Optional[pd.DataFrame]:
    """
    Resample + run full S6 detection suite. Returns None on error.

    Causal behaviour in live:
      detect_swings uses centered window. In a live window of N bars,
      bars [N-10 .. N-1] have NaN is_swing_low/high — no future data.
      ssl/bsl used by detect_liq_sweeps are forward-filled from confirmed
      swings, so they are at least 10+ bars old. CISD follows confirmed
      sweeps only. No look-ahead in production.
    """
    try:
        df = df_1min.copy() if tf == 1 else bt.resample_ohlcv(df_1min, tf)
        df = bt.detect_swings(df)
        df = bt.detect_fvgs(df)
        df = bt.detect_ifvgs(df)
        df = bt.detect_liq_sweeps(df)
        df = bt.detect_cisd(df)
        return df
    except Exception as exc:
        logging.warning("[_prep_s6] %dmin failed: %s: %s", tf, type(exc).__name__, exc)
        return None


def scan_symbol_s6(
    symbol: str,
    df_1min: pd.DataFrame,
    strategy_cfg: dict,
    session_windows,
) -> list:
    """
    Scan one symbol for S6 signals.

    Returns list of Signal objects (0 or 1 per CISD event found in window).
    """
    htf_tf = strategy_cfg.get("htf_tf", 60)
    ltf    = strategy_cfg["timeframes"][0]  # e.g. 3 min

    # ── Step 1: HTF delivery map ──────────────────────────────────────────────
    try:
        htf_map, htf_sorted = bt.build_s6_htf_delivery(df_1min, htf_tf)
    except Exception:
        return []
    if not htf_sorted:
        return []

    # ── Step 2: LTF with S6 detections ───────────────────────────────────────
    ltf_df = _prep_s6(df_1min, ltf)
    if ltf_df is None or len(ltf_df) < 50:
        return []

    signals   = []
    n         = len(ltf_df)
    scan_from = max(0, n - CISD_SCAN_WINDOW)

    for start_i in range(scan_from, n - 1):
        bar   = ltf_df.iloc[start_i]
        is_bull = bool(bar.get("cisd_bull", False))
        is_bear = bool(bar.get("cisd_bear", False))
        if not (is_bull or is_bear):
            continue

        direction = "bull" if is_bull else "bear"
        cisd_ts   = pd.Timestamp(bar["ts"])

        # ── HTF delivery must align ───────────────────────────────────────────
        htf_idx = bisect.bisect_right(htf_sorted, cisd_ts) - 1
        if htf_idx < 0:
            continue
        htf_dir = htf_map.get(htf_sorted[htf_idx])
        if htf_dir != direction:
            continue

        # ── Scan forward from CISD bar for IFVG retest entry ─────────────────
        for j in range(start_i + 1, n):
            entry_bar = ltf_df.iloc[j]
            entry_ts  = pd.Timestamp(entry_bar["ts"])

            # Session filter
            if not _in_session(entry_ts, session_windows):
                continue

            if direction == "bull":
                ifvg_top = entry_bar.get("ifvg_bull_top", np.nan)
                ifvg_bot = entry_bar.get("ifvg_bull_bot", np.nan)
                if pd.isna(ifvg_top) or pd.isna(ifvg_bot):
                    continue
                ifvg_top, ifvg_bot = float(ifvg_top), float(ifvg_bot)
                # Bar dips into zone (low ≤ zone_top) and closes inside (close ≥ zone_bot)
                if not (entry_bar["low"] <= ifvg_top and entry_bar["close"] >= ifvg_bot):
                    continue
                entry_price = float(entry_bar["close"])
                sl_price    = ifvg_bot
                tp_price    = entry_price + 2.0 * (entry_price - sl_price)
            else:
                ifvg_top = entry_bar.get("ifvg_bear_top", np.nan)
                ifvg_bot = entry_bar.get("ifvg_bear_bot", np.nan)
                if pd.isna(ifvg_top) or pd.isna(ifvg_bot):
                    continue
                ifvg_top, ifvg_bot = float(ifvg_top), float(ifvg_bot)
                # Bar spikes into zone (high ≥ zone_bot) and closes inside (close ≤ zone_top)
                if not (entry_bar["high"] >= ifvg_bot and entry_bar["close"] <= ifvg_top):
                    continue
                entry_price = float(entry_bar["close"])
                sl_price    = ifvg_top
                tp_price    = entry_price - 2.0 * (sl_price - entry_price)

            risk = abs(entry_price - sl_price)
            if risk <= 0:
                continue

            sig = Signal(
                timestamp   = entry_ts.to_pydatetime(),
                symbol      = symbol,
                strategy    = strategy_cfg.get("name", "S6"),
                direction   = direction,
                entry_tf    = ltf,
                entry_price = round(entry_price, 2),
                sl_price    = round(sl_price,    2),
                tp_price    = round(tp_price,    2),
                fvg_top     = round(ifvg_top,    2),
                fvg_bottom  = round(ifvg_bot,    2),
                pd_zone     = "discount" if direction == "bull" else "premium",
                htf_signal  = "delivery",
                htf_tf      = htf_tf,
                risk_pts    = round(risk, 2),
            )
            signals.append(sig)
            break   # one entry per CISD trigger; move to next CISD event

    return signals
