"""
data_guardian.py — Data Quality & Gap Protection
=================================================
Runs alongside the live engine every bar cycle. Handles:

  1. Gap Detection    — finds missing bars per symbol
  2. Gap Backfill     — fetches missing bars from Kite historical API
  3. Retroactive Exit — checks if SL/TP was hit during a gap
  4. Stale Data       — flags symbols with no tick for > 2 minutes
  5. Tick Rate        — monitors ticks/min; suppresses signals on thin data
  6. Signal Expiry    — cancels signals if price moved > threshold or > N bars elapsed
  7. Warmup Guard     — blocks scanning until symbol has enough bars

All events logged via event_logger. Metrics available at /api/health?detail=1.
"""

import os
import time
import threading
from datetime import datetime, timedelta
from collections import defaultdict, deque

import pandas as pd

import event_logger as elog

# ── Config ─────────────────────────────────────────────────────────────────────
SIGNAL_EXPIRY_BARS    = int(os.environ.get("SIGNAL_EXPIRY_BARS", "3"))
SIGNAL_EXPIRY_PCT     = float(os.environ.get("SIGNAL_EXPIRY_PCT", "0.003"))   # 0.3%
STALE_SECONDS         = int(os.environ.get("STALE_SECONDS", "120"))           # 2 min
MIN_TICKS_PER_MIN     = int(os.environ.get("MIN_TICKS_PER_MIN", "1"))
# Lowered from 10 → 1: MODE_LTP only sends ticks on price change, not every second.
# Less liquid stocks may have fewer than 10 price changes/min even when actively trading.
# Stale check (STALE_SECONDS=120) already handles the "no data" case.
MIN_WARMUP_BARS       = int(os.environ.get("MIN_WARMUP_BARS", "50"))
LARGE_GAP_MINUTES     = int(os.environ.get("LARGE_GAP_MINUTES", "15"))
# Circuit breaker: if price unchanged for N consecutive bars → frozen (#48)
# Raised from 5 → 20: price consolidating for 5 min is normal. 20 min of
# exactly same close price is a strong signal of a genuine NSE circuit limit.
CIRCUIT_BREAKER_BARS  = int(os.environ.get("CIRCUIT_BREAKER_BARS", "20"))


class SymbolHealth:
    """Per-symbol data health state."""
    __slots__ = [
        "last_bar_ts", "last_tick_at", "tick_count_window",
        "warmed_up", "stale", "gap_count", "bars_received",
        "circuit_breaker", "frozen_bars", "last_price",   # #48
    ]
    def __init__(self):
        self.last_bar_ts       = None          # pd.Timestamp of last completed bar
        self.last_tick_at      = None          # datetime of last tick received
        self.tick_count_window = deque()       # (datetime, count) rolling 60s window
        self.warmed_up         = False
        self.stale             = False
        self.gap_count         = 0
        self.bars_received     = 0
        self.circuit_breaker   = False         # #48: True if price frozen on circuit
        self.frozen_bars       = 0             # consecutive bars with no price change
        self.last_price        = None          # last known close price


class PendingSignal:
    """A signal waiting to be entered, subject to expiry."""
    __slots__ = ["symbol", "direction", "signal_price", "sl", "tp",
                 "fired_at_bar", "fired_at_ts", "strategy", "tf"]
    def __init__(self, symbol, direction, signal_price, sl, tp,
                 fired_at_bar, fired_at_ts, strategy, tf):
        self.symbol        = symbol
        self.direction     = direction
        self.signal_price  = signal_price
        self.sl            = sl
        self.tp            = tp
        self.fired_at_bar  = fired_at_bar    # bar index when signal fired
        self.fired_at_ts   = fired_at_ts
        self.strategy      = strategy
        self.tf            = tf


class DataGuardian:
    """
    Attach one instance to the live engine.
    Call these methods at the appropriate points in app.py:

      guardian.on_tick(symbol, price, ts)          — every tick
      guardian.on_bar_close(symbol, bar_ts)        — every completed 1min bar
      guardian.check_gap(symbol, kite, bar_window) — after on_bar_close
      guardian.register_signal(sig, bar_idx)       — when signal fires
      guardian.is_signal_valid(symbol, current_price, current_bar_idx)
      guardian.can_scan(symbol, bar_window)        — before scan_symbol()
      guardian.retroactive_sl_tp(gap_bars, tracker, db) — after large gap fill
      guardian.snapshot()                          — for /api/health
    """

    def __init__(self):
        self._health: dict[str, SymbolHealth] = defaultdict(SymbolHealth)
        self._pending: dict[str, PendingSignal] = {}    # symbol -> pending signal
        self._lock = threading.Lock()
        self._stats = {
            "signals_fired": 0,
            "signals_expired_time": 0,
            "signals_expired_price": 0,
            "gaps_detected": 0,
            "gaps_filled": 0,
            "retro_exits": 0,
            "stale_events": 0,
        }

    # ── Tick tracking ───────────────────────────────────────────────────────────

    def on_tick(self, symbol: str, price: float, ts: datetime):
        """Call on every received tick for a symbol."""
        h = self._health[symbol]
        now = datetime.now()
        h.last_tick_at = now
        h.stale = False

        # Rolling 60s tick counter
        h.tick_count_window.append(now)
        cutoff = now - timedelta(seconds=60)
        while h.tick_count_window and h.tick_count_window[0] < cutoff:
            h.tick_count_window.popleft()

    def on_bar_close(self, symbol: str, bar_ts: pd.Timestamp, bar: dict = None):
        """Call when a 1-min bar completes for a symbol."""
        h = self._health[symbol]
        h.last_bar_ts = bar_ts
        h.bars_received += 1
        if h.bars_received >= MIN_WARMUP_BARS:
            h.warmed_up = True
        # Circuit breaker detection (#48)
        if bar is not None:
            self._check_circuit_breaker(symbol, h, bar)

    def _check_circuit_breaker(self, symbol: str, h: SymbolHealth, bar: dict):
        """
        Detect NSE circuit breaker: price unchanged for CIRCUIT_BREAKER_BARS
        consecutive bars → flag and suppress signals. (#48)

        NOTE: Volume check removed — in MODE_LTP the WebSocket tick has no
        per-tick "volume" field (only "volume_traded" cumulative), so bar.volume
        is always 0 and the volume_frozen condition was permanently True,
        causing spurious circuit breakers for any stock consolidating ≥5 bars.
        Price-only freeze detection with the configurable bar threshold is
        sufficient to catch genuine NSE circuit limits (price truly stuck).
        """
        close = bar.get("close", 0)

        price_frozen = (h.last_price is not None and close == h.last_price)

        if price_frozen:
            h.frozen_bars += 1
        else:
            h.frozen_bars = 0
            if h.circuit_breaker:
                h.circuit_breaker = False
                elog.info("CIRCUIT_BREAKER_CLEARED",
                          f"{symbol}: price movement resumed",
                          {"symbol": symbol})

        if not h.circuit_breaker and h.frozen_bars >= CIRCUIT_BREAKER_BARS:
            h.circuit_breaker = True
            elog.warn("CIRCUIT_BREAKER_DETECTED",
                      f"{symbol}: price frozen at ₹{close:.2f} for "
                      f"{h.frozen_bars} bars — circuit breaker suspected, signals suppressed",
                      {"symbol": symbol, "price": close, "frozen_bars": h.frozen_bars})

        h.last_price = close

    # ── Gap detection & backfill ────────────────────────────────────────────────

    def check_gap(self, symbol: str, kite, bar_window,
                  tracker=None, db_module=None) -> bool:
        """
        Compare last known bar timestamp with expected current bar.
        If gap found, backfill from Kite historical.
        Returns True if gap was detected (and handled).
        """
        h = self._health[symbol]
        if h.last_bar_ts is None:
            return False

        expected_next = h.last_bar_ts + pd.Timedelta(minutes=1)
        now_floor = pd.Timestamp(datetime.now()).floor("1min")

        if now_floor <= expected_next:
            return False   # no gap

        gap_minutes = int((now_floor - expected_next).total_seconds() / 60)
        if gap_minutes < 1:
            return False

        self._stats["gaps_detected"] += 1
        h.gap_count += 1
        elog.warn("GAP_DETECTED",
                  f"{symbol}: {gap_minutes} bar(s) missing "
                  f"({expected_next.strftime('%H:%M')} → {now_floor.strftime('%H:%M')})",
                  {"symbol": symbol, "gap_minutes": gap_minutes,
                   "from": str(expected_next), "to": str(now_floor)})

        # Backfill from Kite
        filled = self._backfill(symbol, kite, bar_window,
                                expected_next.to_pydatetime(),
                                now_floor.to_pydatetime())
        if filled:
            self._stats["gaps_filled"] += 1
            # For large gaps: check retroactive SL/TP
            if gap_minutes >= LARGE_GAP_MINUTES and tracker and db_module:
                self._retroactive_check(symbol, bar_window, tracker, db_module)

        return True

    def _backfill(self, symbol: str, kite, bar_window,
                  from_dt: datetime, to_dt: datetime) -> bool:
        """Fetch missing bars from Kite historical and insert into bar_window."""
        try:
            from kite_warmer import _get_token
            token = _get_token(kite, symbol)
            if not token:
                return False

            candles = kite.historical_data(
                token, from_date=from_dt, to_date=to_dt,
                interval="minute", continuous=False, oi=False,
            )
            if not candles:
                return False

            df = pd.DataFrame(candles)
            df = df.rename(columns={"date": "ts"})
            df["ts"] = pd.to_datetime(df["ts"])
            df = df[["ts", "open", "high", "low", "close", "volume"]]

            # Insert each missing bar into window
            for _, row in df.iterrows():
                bar_dict = row.to_dict()
                bar_dict["ts"] = row["ts"]
                bar_window.push(symbol, bar_dict)
                self.on_bar_close(symbol, row["ts"])

            elog.info("GAP_FILLED",
                      f"{symbol}: backfilled {len(df)} bar(s) from Kite",
                      {"symbol": symbol, "bars_filled": len(df),
                       "from": str(from_dt), "to": str(to_dt)})
            return True

        except Exception as exc:
            elog.error("GAP_FILL_FAIL",
                       f"{symbol}: backfill failed — {exc}", exc=exc,
                       data={"symbol": symbol})
            return False

    def _retroactive_check(self, symbol: str, bar_window,
                           tracker, db_module):
        """
        For positions open during a large gap: check if SL or TP
        was touched in the backfilled bars and apply exit retroactively.
        """
        pos = tracker.open_positions.get(symbol)
        if not pos:
            return

        df = bar_window.get(symbol)
        if df is None or len(df) < 2:
            return

        entry_time = pd.Timestamp(pos.entry_time)
        gap_bars = df[df["ts"] > entry_time].tail(LARGE_GAP_MINUTES + 5)

        for _, bar in gap_bars.iterrows():
            # Check SL (for bull: low touches SL; for bear: high touches SL)
            if pos.direction == "bull" and bar["low"] <= pos.sl_price:
                _apply_retro_exit(symbol, pos, bar, "SL", pos.sl_price,
                                  tracker, db_module)
                self._stats["retro_exits"] += 1
                elog.warn("RETRO_EXIT",
                          f"{symbol} SL hit during gap at {bar['ts']} "
                          f"low={bar['low']:.2f} SL={pos.sl_price:.2f}",
                          {"symbol": symbol, "reason": "SL",
                           "bar_ts": str(bar["ts"])})
                return
            if pos.direction == "bear" and bar["high"] >= pos.sl_price:
                _apply_retro_exit(symbol, pos, bar, "SL", pos.sl_price,
                                  tracker, db_module)
                self._stats["retro_exits"] += 1
                return

            # Check TP
            if pos.direction == "bull" and bar["high"] >= pos.tp_price:
                _apply_retro_exit(symbol, pos, bar, "TP", pos.tp_price,
                                  tracker, db_module)
                self._stats["retro_exits"] += 1
                elog.info("RETRO_EXIT",
                          f"{symbol} TP hit during gap at {bar['ts']}",
                          {"symbol": symbol, "reason": "TP"})
                return
            if pos.direction == "bear" and bar["low"] <= pos.tp_price:
                _apply_retro_exit(symbol, pos, bar, "TP", pos.tp_price,
                                  tracker, db_module)
                self._stats["retro_exits"] += 1
                return

    # ── Stale data check ────────────────────────────────────────────────────────

    def check_stale(self, symbol: str) -> bool:
        """
        Returns True if symbol is stale (no tick for STALE_SECONDS).
        Call this before opening a new position.
        """
        h = self._health[symbol]
        if h.last_tick_at is None:
            return True
        elapsed = (datetime.now() - h.last_tick_at).total_seconds()
        if elapsed > STALE_SECONDS and not h.stale:
            h.stale = True
            self._stats["stale_events"] += 1
            elog.warn("STALE_DATA",
                      f"{symbol}: no tick for {elapsed:.0f}s — "
                      "suppressing signals until data resumes",
                      {"symbol": symbol, "seconds_silent": round(elapsed)})
        elif elapsed <= STALE_SECONDS:
            h.stale = False
        return h.stale

    def ticks_per_min(self, symbol: str) -> int:
        return len(self._health[symbol].tick_count_window)

    def data_quality_ok(self, symbol: str) -> bool:
        """True if not stale and no circuit breaker active.

        Tick-rate check intentionally removed: in MODE_LTP the WebSocket only
        sends events on price change, so ticks/min tracks price-change frequency
        not data health. Stale check (STALE_SECONDS=120) covers the 'no data'
        case cleanly. MIN_TICKS_PER_MIN is kept as config but only logged, not
        used to block scans.
        """
        h = self._health[symbol]
        # Circuit breaker suppresses signals (#48)
        if h.circuit_breaker:
            return False
        # Log tick rate for observability without blocking
        tpm = self.ticks_per_min(symbol)
        if tpm < MIN_TICKS_PER_MIN:
            elog.warn("DATA_QUALITY",
                      f"{symbol}: low tick rate {tpm}/min (min={MIN_TICKS_PER_MIN}) — monitoring only",
                      {"symbol": symbol, "ticks_per_min": tpm})
        return not self.check_stale(symbol)

    # ── Signal expiry ───────────────────────────────────────────────────────────

    def register_signal(self, sig, bar_idx: int):
        """Register a newly fired signal for expiry tracking."""
        self._stats["signals_fired"] += 1
        self._pending[sig.symbol] = PendingSignal(
            symbol=sig.symbol, direction=sig.direction,
            signal_price=sig.entry_price, sl=sig.sl_price, tp=sig.tp_price,
            fired_at_bar=bar_idx, fired_at_ts=sig.timestamp,
            strategy=sig.strategy, tf=sig.entry_tf,
        )

    def consume_signal(self, symbol: str):
        """Call when a position is successfully opened. Removes pending signal."""
        self._pending.pop(symbol, None)

    def is_signal_valid(self, symbol: str, current_price: float,
                        current_bar_idx: int) -> tuple[bool, str]:
        """
        Check if pending signal for symbol is still valid.
        Returns (valid, reason).
        """
        ps = self._pending.get(symbol)
        if not ps:
            return True, "no_pending"

        # Time expiry
        bars_elapsed = current_bar_idx - ps.fired_at_bar
        if bars_elapsed > SIGNAL_EXPIRY_BARS:
            self._pending.pop(symbol, None)
            self._stats["signals_expired_time"] += 1
            elog.warn("SIGNAL_EXPIRED",
                      f"{symbol} signal expired — {bars_elapsed} bars elapsed "
                      f"(max={SIGNAL_EXPIRY_BARS})",
                      {"symbol": symbol, "bars_elapsed": bars_elapsed,
                       "signal_price": ps.signal_price, "reason": "time"})
            return False, "time_expiry"

        # Price drift expiry
        if ps.signal_price and ps.signal_price > 0:
            drift = abs(current_price - ps.signal_price) / ps.signal_price
            if drift > SIGNAL_EXPIRY_PCT:
                self._pending.pop(symbol, None)
                self._stats["signals_expired_price"] += 1
                elog.warn("SIGNAL_EXPIRED",
                          f"{symbol} signal expired — price drifted "
                          f"{drift*100:.2f}% from ₹{ps.signal_price:.2f}",
                          {"symbol": symbol, "drift_pct": round(drift*100, 2),
                           "signal_price": ps.signal_price,
                           "current_price": current_price, "reason": "price_drift"})
                return False, "price_drift"

        return True, "ok"

    # ── Warmup guard ────────────────────────────────────────────────────────────

    def can_scan(self, symbol: str, bar_window) -> bool:
        """
        Returns True only if symbol has enough bars and data quality is ok.
        Guards scan_symbol() from running on insufficient data.
        """
        h = self._health[symbol]
        if not h.warmed_up:
            df = bar_window.get(symbol)
            bars = len(df) if df is not None else 0
            if bars < MIN_WARMUP_BARS:
                return False
            h.warmed_up = True   # graduated to warmed up
            elog.info("WARMUP_READY",
                      f"{symbol}: {bars} bars collected — scanning active",
                      {"symbol": symbol, "bars": bars})
        return True

    # ── Snapshot for /api/health ────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Return per-symbol health + aggregate stats for API."""
        now = datetime.now()
        symbols = {}
        for sym, h in self._health.items():
            last_tick_ago = None
            if h.last_tick_at:
                last_tick_ago = round((now - h.last_tick_at).total_seconds())
            symbols[sym] = {
                "warmed_up":       h.warmed_up,
                "stale":           h.stale,
                "circuit_breaker": h.circuit_breaker,
                "frozen_bars":     h.frozen_bars,
                "ticks_per_min":   len(h.tick_count_window),
                "gap_count":       h.gap_count,
                "bars_received":   h.bars_received,
                "last_tick_ago_s": last_tick_ago,
                "health": (
                    "RED"    if h.stale or h.circuit_breaker else
                    "YELLOW" if len(h.tick_count_window) < MIN_TICKS_PER_MIN else
                    "GREEN"
                ),
            }
        total = self._stats["signals_fired"] or 1
        expiry_rate = round(
            (self._stats["signals_expired_time"] +
             self._stats["signals_expired_price"]) / total * 100, 1
        )
        return {
            "symbols": symbols,
            "stats": {**self._stats, "signal_expiry_rate_pct": expiry_rate},
        }


# ── Helper: retroactive exit ───────────────────────────────────────────────────

def _apply_retro_exit(symbol, pos, bar, reason, exit_price, tracker, db_module):
    """Apply a retroactive SL/TP exit found during gap backfill."""
    exit_time = bar["ts"] if hasattr(bar["ts"], "isoformat") else datetime.now()
    pnl_pts   = (exit_price - pos.entry_price) * (1 if pos.direction == "bull" else -1)
    pnl_rs    = pnl_pts / pos.entry_price * 200_000   # position size
    win       = 1 if pnl_rs > 0 else 0

    # Remove from tracker
    tracker.open_positions.pop(symbol, None)

    # Update DB if trade_id known
    if hasattr(pos, "trade_id") and pos.trade_id and db_module:
        try:
            db_module.close_trade(pos.trade_id, {
                "exit_time":   exit_time.isoformat() if hasattr(exit_time, "isoformat") else str(exit_time),
                "exit_price":  exit_price,
                "exit_reason": f"{reason}_GAP",
                "pnl_pts":     round(pnl_pts, 4),
                "pnl_pct":     round(pnl_pts / pos.entry_price * 100, 4),
                "pnl_rs":      round(pnl_rs, 2),
                "win":         win,
            })
        except Exception as exc:
            elog.error("ERROR", f"retro exit DB update failed: {exc}", exc=exc)

    elog.position_close(symbol, pos.direction, pos.entry_price,
                        exit_price, f"{reason}_GAP", round(pnl_rs, 2))
