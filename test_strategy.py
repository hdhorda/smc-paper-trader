"""
test_strategy.py — Pipeline Verification Strategy
==================================================
⚠️  TEST ONLY — disable once live pipeline is confirmed working.

Strategy: 5-min Opening Range Breakout (ORB)
  - Opening Range = first 5-min bar of the day (09:15 candle)
  - Signal fires when any subsequent bar closes outside the ORB
  - Fires on HDFCBANK / SBIN / INFY — liquid stocks NOT in S4A/S5/S6 watchlists
  - Fires within the first 30-60 min of almost every trading day

Purpose:
  Verify end-to-end: signal → paper trade open → position tracking
  → SL/TP/EOD exit → P&L calculation → Telegram alert → dashboard.

Disable by setting  enabled: False  in app.py STRATEGIES dict once
at least one trade is confirmed to have gone through the full pipeline.
"""

import sys
import logging
import warnings
from pathlib import Path
from datetime import datetime
from typing import Optional

import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")

_P8 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_P8))
import smc_backtest as bt

from signal_engine import Signal, _in_session


def scan_symbol_test(
    symbol: str,
    df_1min: pd.DataFrame,
    strategy_cfg: dict,
    session_windows,
) -> list:
    """
    5-min ORB: first bar of the day defines the Opening Range.
    First subsequent bar that closes outside the range → signal.
    """
    try:
        df5 = bt.resample_ohlcv(df_1min, 5)
        df5 = bt.apply_session_filter(df5, "full")
        df5 = df5.reset_index(drop=True)
    except Exception as exc:
        logging.warning("[test_strategy] prep failed for %s: %s", symbol, exc)
        return []

    if len(df5) < 2:
        return []

    # Today's session bars only
    last_ts = pd.Timestamp(df5["ts"].iloc[-1])
    today   = last_ts.date()
    today_df = df5[df5["ts"].dt.date == today].reset_index(drop=True)

    if len(today_df) < 2:
        return []

    # First bar must be the 09:15 open
    orb_bar = today_df.iloc[0]
    if orb_bar["ts"].hour != 9 or orb_bar["ts"].minute != 15:
        return []

    orb_high = float(orb_bar["high"])
    orb_low  = float(orb_bar["low"])
    if orb_high <= orb_low:
        return []

    # Scan the last 3 bars of today for a breakout (dedup prevents re-fire)
    for _, bar in today_df.iloc[1:].tail(3).iterrows():
        ts    = pd.Timestamp(bar["ts"])
        close = float(bar["close"])

        if not _in_session(ts, session_windows):
            continue

        if close > orb_high:
            direction   = "bull"
            entry_price = close
            sl_price    = orb_low
        elif close < orb_low:
            direction   = "bear"
            entry_price = close
            sl_price    = orb_high
        else:
            continue

        risk = abs(entry_price - sl_price)
        if risk <= 0:
            continue

        tp_price = (entry_price + 2.0 * risk) if direction == "bull" \
                   else (entry_price - 2.0 * risk)

        return [Signal(
            timestamp   = ts.to_pydatetime(),
            symbol      = symbol,
            strategy    = strategy_cfg.get("name", "TEST_ORB5"),
            direction   = direction,
            entry_tf    = 5,
            entry_price = round(entry_price, 2),
            sl_price    = round(sl_price,    2),
            tp_price    = round(tp_price,    2),
            fvg_top     = round(orb_high,    2),
            fvg_bottom  = round(orb_low,     2),
            pd_zone     = "unknown",
            risk_pts    = round(risk,         2),
        )]

    return []
