"""
Signal Engine
=============
Every minute: takes the rolling 1min DataFrame per symbol,
resamples to needed TFs, runs all detections, checks entry conditions.

Returns Signal objects when a valid setup is detected.
"""

import sys
import warnings
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")

# Load smc_backtest from Project_8
_P8 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_P8))
import smc_backtest as bt

# ── Signal dataclass ──────────────────────────────────────────────────────────

@dataclass
class Signal:
    timestamp:   datetime
    symbol:      str
    strategy:    str
    direction:   str          # 'bull' or 'bear'
    entry_tf:    int          # minutes
    entry_price: float
    sl_price:    float
    tp_price:    float
    fvg_top:     float
    fvg_bottom:  float
    pd_zone:     str
    htf_signal:  Optional[str] = None   # 'ob'/'bos'/'choch' or None
    htf_tf:      Optional[int] = None
    risk_pts:    float = 0.0
    rr_ratio:    float = 2.0

    def summary(self) -> str:
        d = "🟢 LONG" if self.direction == "bull" else "🔴 SHORT"
        return (
            f"[{self.strategy}] {d} {self.symbol} @ ₹{self.entry_price:.2f} | "
            f"SL ₹{self.sl_price:.2f} | TP ₹{self.tp_price:.2f} | "
            f"TF {self.entry_tf}min | {self.timestamp.strftime('%H:%M')}"
        )


# ── Detection helpers ──────────────────────────────────────────────────────────

def _prep(df_1min: pd.DataFrame, tf: int) -> Optional[pd.DataFrame]:
    """
    Resample and run all detections. Returns None on error.

    ── Look-ahead bias audit (task #37) ────────────────────────────────────────
    RESULT: NO LOOK-AHEAD BIAS. Verified 2026-06-29.

    1. detect_swings: uses rolling(2*n+1, center=True) with n=SWING_LOOKBACK=10.
       → Last 10 bars have NaN swing status in live mode (future bars don't exist).
       → No live signal can rely on a swing that needs future bars.

    2. detect_choch: explicitly shifts bias by 1 bar (.shift(1)).
       → Signal at bar[-1] uses CHoCH status from bar[-2], not bar[-1] itself.
       → No same-bar look-ahead.

    3. detect_bos_choch: also shifted 1 bar (documented in smc_backtest.py).

    4. FVG / Liq-grab / OB: all purely candle-pattern detection using
       current and prior bars only. No future reference.

    5. scan_symbol uses tail(3): safe because bars [-1]/[-2]/[-3] don't have
       live swing confirmation (needs 10 future bars), so all signals rely on
       swings confirmed 10+ bars ago.

    Minor delay: CHoCH fires at bar i+1 after a structural break at bar i
    (1-bar shift). Live system has same 1-bar delay as backtest. ✓
    ─────────────────────────────────────────────────────────────────────────────
    """
    try:
        df = df_1min.copy() if tf == 1 else bt.resample_ohlcv(df_1min, tf)
        df = bt.detect_swings(df)
        df = bt.detect_liquidity_grabs(df)
        df = bt.detect_fvgs(df)
        df = bt.detect_choch(df)
        df = bt.detect_bos_choch(df)
        df = bt.detect_order_blocks(df)
        df = bt.add_pd_zones(df)
        return df
    except Exception:
        return None


def _in_session(ts: pd.Timestamp, windows) -> bool:
    t = ts.time()
    for start_str, end_str in windows:
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        from datetime import time as dtime
        if dtime(sh, sm) <= t <= dtime(eh, em):
            return True
    return False


# ── Main scan function ─────────────────────────────────────────────────────────

def scan_symbol(
    symbol:       str,
    df_1min:      pd.DataFrame,
    strategy_cfg: dict,
    session_windows,
) -> list[Signal]:
    """
    Scan one symbol for new signals at the current bar.
    Returns list of Signal objects (usually 0 or 1 per TF).
    """
    signals = []
    htf_signal_type = strategy_cfg.get("htf_signal")
    htf_tf          = strategy_cfg.get("htf_tf")

    # Build HTF map if needed (S5)
    htf_map = htf_ts_sorted = None
    if htf_signal_type and htf_tf:
        htf_df = _prep(df_1min, htf_tf)
        if htf_df is not None:
            htf_map       = bt.compute_htf_signal_map(htf_df, htf_signal_type)
            htf_ts_sorted = sorted(htf_map.keys())

    for ltf in strategy_cfg["timeframes"]:
        if htf_tf and ltf >= htf_tf:
            continue  # LTF must be strictly less than HTF

        ltf_df = _prep(df_1min, ltf)
        if ltf_df is None or len(ltf_df) < 10:
            continue

        # Only look at the last 2 bars for new signals (avoid re-alerting old ones)
        last_bars = ltf_df.tail(3)

        for _, bar in last_bars.iterrows():
            ts = pd.Timestamp(bar["ts"])

            # Session filter
            if not _in_session(ts, session_windows):
                continue

            # FVG present?
            fvg_top = bar.get("fvg_top", np.nan)
            fvg_bot = bar.get("fvg_bottom", np.nan)
            if pd.isna(fvg_top) or pd.isna(fvg_bot):
                continue

            # CHoCH bias
            choch = bar.get("choch_bias", "none")
            if choch not in ("bull", "bear"):
                continue

            direction = choch

            # PD zone check
            pd_zone = bar.get("pd_zone", "unknown")
            if direction == "bull" and pd_zone not in ("discount", "unknown"):
                continue
            if direction == "bear" and pd_zone not in ("premium", "unknown"):
                continue

            # Liquidity grab present?
            liq_grab = bar.get("liq_grab_bull" if direction == "bull" else "liq_grab_bear", False)
            # liq_grab column name may vary — check both
            for col in ["liq_grab_bull", "liq_grab_bear", "liq_grab"]:
                if col in bar.index and bar[col]:
                    liq_grab = True
                    break

            # HTF filter (S5)
            if htf_map is not None:
                import bisect
                idx = bisect.bisect_right(htf_ts_sorted, ts) - 1
                if idx < 0:
                    continue
                htf_dir = htf_map.get(htf_ts_sorted[idx])
                if htf_dir != direction:
                    continue

            # Build signal
            close = float(bar["close"])
            if direction == "bull":
                entry = float(fvg_bot)
                sl    = float(fvg_bot) - (float(fvg_top) - float(fvg_bot))
                tp    = entry + 2.0 * (entry - sl)
            else:
                entry = float(fvg_top)
                sl    = float(fvg_top) + (float(fvg_top) - float(fvg_bot))
                tp    = entry - 2.0 * (sl - entry)

            risk = abs(entry - sl)
            if risk <= 0:
                continue

            sig = Signal(
                timestamp=ts.to_pydatetime(),
                symbol=symbol,
                strategy=strategy_cfg.get("name", "S?"),
                direction=direction,
                entry_tf=ltf,
                entry_price=round(entry, 2),
                sl_price=round(sl, 2),
                tp_price=round(tp, 2),
                fvg_top=round(float(fvg_top), 2),
                fvg_bottom=round(float(fvg_bot), 2),
                pd_zone=pd_zone,
                htf_signal=htf_signal_type,
                htf_tf=htf_tf,
                risk_pts=round(risk, 2),
            )
            signals.append(sig)

    return signals
