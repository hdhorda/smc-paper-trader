"""
Signal Engine
=============
Replicates run_backtest() from smc_backtest.py in a live/rolling context.

Strategy: Liquidity Grab → FVG Formation → FVG Retest (same as backtest)
--------------------------------------------------------------------------
For the last N bars, scan_symbol checks whether any of them satisfies the
exact entry conditions used in run_backtest():

  1. A grab of the right direction exists within FVG_LOOKFORWARD bars before
     the FVG (can be on a previous session day — backtest crosses overnight).

  2. The first FVG of the matching direction after the grab is found within
     FVG_LOOKFORWARD bars of the grab.

  3. The entry bar is between [fvg_idx+2 … fvg_idx+ENTRY_LOOKFORWARD] with
     no zone-invalidation candle in between.

  4. Price-in-zone entry (same as backtest):
       Bull: prev_close > fvg_top  AND  low  <= fvg_top  AND  close >= fvg_bot
       Bear: prev_close < fvg_bot  AND  high >= fvg_bot  AND  close <= fvg_top

  5. Filters applied at the entry bar: PD zone, CHoCH bias, session window,
     HTF OB alignment (S5).

Critical: apply_session_filter MUST be called after resampling (same as
run_backtest in smc_backtest.py). Without it, extra non-session bars in
historical parquet files shift bar indices, breaking the grab→FVG distance
checks and producing different FVG detection results (cross-session FVGs
change when the "previous bar" changes after filtering).

Root cause of previous zero-signal bug:
  - Old engine used CHoCH as the primary trigger, not the grab.
  - _prep() did not call apply_session_filter(), producing wrong bar indices.

S6 strategy uses a separate engine (s6_strategy.scan_symbol_s6()) — unaffected.
"""

import sys
import logging
import warnings
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")

_P8 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_P8))
import smc_backtest as bt

# Candidate rejection logger — records why a price-in-zone candidate was blocked
# by a meta-filter. Imported lazily to avoid any circular-import risk.
try:
    from signal_pipeline import log_candidate_reject as _log_reject
except ImportError:
    def _log_reject(*a, **k): pass  # graceful degradation if file missing

# Mirror constants from smc_backtest
FVG_LOOKFORWARD   = getattr(bt, "FVG_LOOKFORWARD",   20)
ENTRY_LOOKFORWARD = getattr(bt, "ENTRY_LOOKFORWARD",  30)
MIN_BARS_TO_CLOSE = getattr(bt, "MIN_BARS_TO_CLOSE",  15)

# Minimum risk as % of entry price.
# FVG zones tighter than this are sub-tick after slippage and not tradeable live.
# Median 3-min FVG width is ~0.06%; 0.2% ≈ 3× round-trip slippage on NSE.
MIN_RISK_PCT = 0.002   # 0.2% of entry price


# ── Signal dataclass ────────────────────────────────────────────────────────

@dataclass
class Signal:
    timestamp:   datetime
    symbol:      str
    strategy:    str
    direction:   str
    entry_tf:    int
    entry_price: float
    sl_price:    float
    tp_price:    float
    fvg_top:     float
    fvg_bottom:  float
    pd_zone:     str
    htf_signal:  Optional[str] = None
    htf_tf:      Optional[int] = None
    risk_pts:    float = 0.0
    rr_ratio:    float = 2.0

    def summary(self) -> str:
        d = "LONG" if self.direction == "bull" else "SHORT"
        return (
            f"[{self.strategy}] {d} {self.symbol} @ {self.entry_price:.2f} | "
            f"SL {self.sl_price:.2f} | TP {self.tp_price:.2f} | "
            f"TF {self.entry_tf}min | {self.timestamp.strftime('%H:%M')}"
        )


# ── Prep ────────────────────────────────────────────────────────────────────

def _prep(df_1min: pd.DataFrame, tf: int) -> Optional[pd.DataFrame]:
    """
    Resample + session-filter + run all detections.

    apply_session_filter is called immediately after resampling — same as
    run_backtest's scan scripts — so bar indices match the backtest exactly.
    This is critical for the Grab → FVG distance calculations.
    """
    try:
        if tf == 1:
            df = df_1min.copy()
        else:
            df = bt.resample_ohlcv(df_1min, tf)

        # Session filter (must match backtest — removes non-session bars
        # and resets index so grab/FVG indices line up with run_backtest)
        df = bt.apply_session_filter(df, "full")
        df = df.reset_index(drop=True)

        df = bt.detect_swings(df)
        df = bt.detect_liquidity_grabs(df)
        df = bt.detect_fvgs(df)
        df = bt.detect_choch(df)
        df = bt.detect_bos_choch(df)
        df = bt.detect_order_blocks(df)
        df = bt.add_pd_zones(df)
        return df
    except Exception as exc:
        logging.warning("[_prep] %dmin failed: %s: %s", tf, type(exc).__name__, exc)
        return None


def _in_session(ts: pd.Timestamp, windows) -> bool:
    from datetime import time as dtime
    t = ts.time()
    for start_str, end_str in windows:
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        if dtime(sh, sm) <= t <= dtime(eh, em):
            return True
    return False


# ── Main scan function ────────────────────────────────────────────────────

def scan_symbol(
    symbol:       str,
    df_1min:      pd.DataFrame,
    strategy_cfg: dict,
    session_windows,
) -> list:
    """
    Scan the last few bars for valid entry signals.

    Replicates run_backtest() logic: Grab → FVG → Retest.
    Returns list of Signal objects (one per valid grab-FVG-entry chain).
    """
    signals = []
    htf_signal_type = strategy_cfg.get("htf_signal")
    htf_tf          = strategy_cfg.get("htf_tf")

    # Build HTF OB map (S5)
    htf_map       = None
    htf_ts_sorted = None
    if htf_signal_type and htf_tf:
        htf_df = _prep(df_1min, htf_tf)
        if htf_df is not None:
            htf_map       = bt.compute_htf_signal_map(htf_df, htf_signal_type)
            htf_ts_sorted = sorted(htf_map.keys())

    for ltf in strategy_cfg["timeframes"]:
        if htf_tf and ltf >= htf_tf:
            continue

        ltf_df = _prep(df_1min, ltf)
        if ltf_df is None or len(ltf_df) < 10:
            continue

        n = len(ltf_df)

        # Extract numpy arrays (same as run_backtest)
        close_arr    = ltf_df["close"].to_numpy(dtype=float)
        low_arr      = ltf_df["low"].to_numpy(dtype=float)
        high_arr     = ltf_df["high"].to_numpy(dtype=float)
        ts_arr       = ltf_df["ts"].to_numpy()

        fvg_top_arr  = ltf_df["fvg_top"].to_numpy(dtype=float)
        fvg_bot_arr  = ltf_df["fvg_bottom"].to_numpy(dtype=float)
        bull_fvg_arr = np.where(ltf_df["fvg_bull"].to_numpy())[0]
        bear_fvg_arr = np.where(ltf_df["fvg_bear"].to_numpy())[0]

        grab_bull_arr = ltf_df["grab_bull"].to_numpy(dtype=bool)
        grab_bear_arr = ltf_df["grab_bear"].to_numpy(dtype=bool)

        pd_zone_arr  = ltf_df["pd_zone"].to_numpy()   if "pd_zone"    in ltf_df.columns else None
        choch_arr    = ltf_df["choch_bias"].to_numpy() if "choch_bias" in ltf_df.columns else None

        # How far back to look for grabs: grab→FVG→entry window
        look_back = FVG_LOOKFORWARD + ENTRY_LOOKFORWARD + 2

        # Scan the last 3 closed bars as potential entry bars
        for entry_k in range(max(n - 3, 2), n):
            ts_entry = pd.Timestamp(ts_arr[entry_k])

            if not _in_session(ts_entry, session_windows):
                continue

            for direction in ("bull", "bear"):
                fvg_arr  = bull_fvg_arr  if direction == "bull" else bear_fvg_arr
                grab_arr = grab_bull_arr if direction == "bull" else grab_bear_arr

                start_search = max(0, entry_k - look_back)
                grab_candidates = np.where(grab_arr[start_search:entry_k])[0] + start_search

                entry_fired = False
                for grab_i in grab_candidates:
                    # First FVG of correct direction after grab
                    j = int(np.searchsorted(fvg_arr, grab_i + 1))
                    if j >= len(fvg_arr):
                        continue
                    fvg_found = int(fvg_arr[j])
                    if fvg_found > grab_i + FVG_LOOKFORWARD:
                        continue

                    # Entry must be in [fvg_found+2 … fvg_found+ENTRY_LOOKFORWARD]
                    if entry_k < fvg_found + 2:
                        continue
                    if entry_k > fvg_found + ENTRY_LOOKFORWARD:
                        continue

                    fvg_top = fvg_top_arr[fvg_found]
                    fvg_bot = fvg_bot_arr[fvg_found]
                    if np.isnan(fvg_top) or np.isnan(fvg_bot):
                        continue

                    # Check zone not invalidated between fvg+2 and entry_k-1
                    invalidated = False
                    for m in range(fvg_found + 2, entry_k):
                        if direction == "bull" and close_arr[m] < fvg_bot:
                            invalidated = True; break
                        elif direction == "bear" and close_arr[m] > fvg_top:
                            invalidated = True; break
                    if invalidated:
                        continue

                    # Price-in-zone entry (exact backtest condition)
                    prev_close = close_arr[entry_k - 1]
                    kh = high_arr[entry_k]
                    kl = low_arr[entry_k]
                    kc = close_arr[entry_k]

                    if direction == "bull":
                        if not (prev_close > fvg_top and kl <= fvg_top and kc >= fvg_bot):
                            continue
                        entry = kc; sl = fvg_bot
                    else:
                        if not (prev_close < fvg_bot and kh >= fvg_bot and kc <= fvg_top):
                            continue
                        entry = kc; sl = fvg_top

                    risk = abs(entry - sl)
                    if risk <= 0:
                        continue
                    if risk < entry * MIN_RISK_PCT:   # 0.2% min — filters sub-tick FVG zones
                        try:
                            import event_logger as _el
                            _el.info("SIGNAL_FILTERED",
                                     f"{symbol} {direction} {ltf}min filtered: risk={risk:.3f}pts "
                                     f"({risk/entry*100:.3f}%) < MIN_RISK_PCT {MIN_RISK_PCT*100:.1f}%",
                                     {"symbol": symbol, "strategy": strategy_cfg.get("name","?"),
                                      "direction": direction, "tf": ltf,
                                      "entry": round(entry, 2), "risk_pts": round(risk, 4),
                                      "risk_pct": round(risk / entry * 100, 4),
                                      "filter": "MIN_RISK_PCT"})
                        except Exception:
                            pass
                        continue

                    # ── Filters at the entry bar ────────────────────────────
                    # NOTE: price IS in zone at this point (FVG retest confirmed).
                    # Rejections below are logged as CANDIDATE_REJECT so we know
                    # exactly which meta-filter blocked an otherwise valid entry.
                    _strat_name = strategy_cfg.get("name", "?")
                    _ts_str     = ts_entry.strftime("%H:%M")

                    # PD zone
                    pd_zone = pd_zone_arr[entry_k] if pd_zone_arr is not None else "unknown"
                    if direction == "bull" and pd_zone not in ("discount", "unknown"):
                        _log_reject(symbol, _strat_name, direction, ltf, _ts_str,
                                    "PD_ZONE", f"zone={pd_zone} need=discount")
                        continue
                    if direction == "bear" and pd_zone not in ("premium", "unknown"):
                        _log_reject(symbol, _strat_name, direction, ltf, _ts_str,
                                    "PD_ZONE", f"zone={pd_zone} need=premium")
                        continue

                    # CHoCH bias
                    if choch_arr is not None:
                        cb = choch_arr[entry_k]
                        if direction == "bull" and cb not in ("bull", "none"):
                            _log_reject(symbol, _strat_name, direction, ltf, _ts_str,
                                        "CHOCH_BIAS", f"bias={cb} need=bull/none")
                            continue
                        if direction == "bear" and cb not in ("bear", "none"):
                            _log_reject(symbol, _strat_name, direction, ltf, _ts_str,
                                        "CHOCH_BIAS", f"bias={cb} need=bear/none")
                            continue

                    # HTF OB alignment (S5)
                    if htf_map is not None:
                        import bisect as _bisect
                        hi = _bisect.bisect_right(htf_ts_sorted, ts_entry) - 1
                        if hi < 0:
                            continue
                        htf_dir = htf_map.get(htf_ts_sorted[hi])
                        if htf_dir != direction:
                            _log_reject(symbol, _strat_name, direction, ltf, _ts_str,
                                        "HTF_OB", f"htf={htf_dir} need={direction}")
                            continue

                    tp = (entry + 2.0 * (entry - sl)) if direction == "bull" \
                         else (entry - 2.0 * (sl - entry))

                    sig = Signal(
                        timestamp=ts_entry.to_pydatetime(),
                        symbol=symbol,
                        strategy=strategy_cfg.get("name", "S?"),
                        direction=direction,
                        entry_tf=ltf,
                        entry_price=round(entry, 2),
                        sl_price=round(sl, 2),
                        tp_price=round(tp, 2),
                        fvg_top=round(fvg_top, 2),
                        fvg_bottom=round(fvg_bot, 2),
                        pd_zone=pd_zone,
                        htf_signal=htf_signal_type,
                        htf_tf=htf_tf,
                        risk_pts=round(risk, 2),
                    )
                    signals.append(sig)
                    entry_fired = True
                    break  # one signal per grab-FVG pair

                if entry_fired:
                    continue

    return signals
