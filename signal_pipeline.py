"""
signal_pipeline.py — Signal audit trail
========================================
SignalContext records every post-scanner gate a signal candidate passes
through, giving complete visibility into why a trade was or was not taken.

Gate sequence in process_bar():
  1. COOLDOWN      — 5-min dedup lock (in_cooldown)
  2. SIGNAL_VALID  — FVG zone still valid (guardian.is_signal_valid)
  3. SIGNAL_LOGGED — signal written to DB and Telegram notified (always PASS)
  4. POSITION_FREE — no existing open trade for this symbol
  5. POSITION_CAP  — under MAX_OPEN_POSITIONS limit
  6. TRADE_OPENED  — position successfully opened (always PASS)

Example trace (blocked):
  SIGNAL_TRACE 20260717_0918_RELIANCE_bull_3m_S4A → BLOCKED@POSITION_FREE: existing position
  gates: [
    {gate:"COOLDOWN",      result:"PASS", reason:""},
    {gate:"SIGNAL_VALID",  result:"PASS", reason:""},
    {gate:"SIGNAL_LOGGED", result:"PASS", reason:""},
    {gate:"POSITION_FREE", result:"FAIL", reason:"existing position"},
  ]

Example trace (trade opened):
  SIGNAL_TRACE 20260717_0918_RELIANCE_bull_3m_S4A → TRADE_OPENED
  gates: [...all PASS..., {gate:"TRADE_OPENED", result:"PASS", reason:""}]

Usage in app.py process_bar():
  ctx = SignalContext.make(sym, sig)
  if not ctx.gate("COOLDOWN", not in_cooldown(...)):
      elog.signal_trace(ctx); continue
  valid, reason = guardian.is_signal_valid(...)
  if not ctx.gate("SIGNAL_VALID", valid, reason or ""):
      elog.signal_trace(ctx); continue
  ctx.gate("SIGNAL_LOGGED", True)
  ...db.insert_signal / elog.signal_fired / tg.signal_fired...
  if not ctx.gate("POSITION_FREE", sym not in tracker.open_positions, "existing position"):
      elog.signal_trace(ctx); continue
  if not ctx.gate("POSITION_CAP", len(tracker.open_positions) < MAX_OPEN_POSITIONS, ...):
      elog.signal_trace(ctx); continue
  opened = tracker.open_position(sig)   # returns True=inserted, False=race/SL-cooldown
  if not opened:
      elog.warn("OPEN_BLOCKED", ...); elog.signal_trace(ctx); continue
  ctx.gate("TRADE_OPENED", True)        # recorded AFTER open so audit trail is accurate
  elog.signal_trace(ctx)

Usage in scanners (candidate rejection AFTER price-in-zone passes):
  from signal_pipeline import log_candidate_reject
  if direction == "bull" and pd_zone not in ("discount", "unknown"):
      log_candidate_reject(symbol, strategy, direction, ltf,
                           ts_entry.strftime("%H:%M"), "PD_ZONE",
                           f"zone={pd_zone} need=discount")
      continue
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SignalContext:
    """Audit trail for one signal candidate from scanner through to trade open."""
    signal_id:  str       # "20260717_0918_RELIANCE_bull_3m_S4A"
    symbol:     str
    strategy:   str
    direction:  str
    tf:         int
    bar_ts:     str       # "09:18"
    gates:      list = field(default_factory=list)
    created_at: str  = field(default_factory=lambda: datetime.now().isoformat())

    @classmethod
    def make(cls, sym: str, sig) -> "SignalContext":
        """Build a SignalContext from a Signal object."""
        ts_str = sig.timestamp.strftime("%Y%m%d_%H%M")
        d_flag = sig.direction   # "bull" / "bear"
        sid    = f"{ts_str}_{sym}_{d_flag}_{sig.entry_tf}m_{sig.strategy}"
        return cls(
            signal_id=sid,  symbol=sym,
            strategy=sig.strategy, direction=sig.direction,
            tf=sig.entry_tf, bar_ts=sig.timestamp.strftime("%H:%M"),
        )

    def gate(self, name: str, passed: bool, reason: str = "") -> bool:
        """
        Record a gate result.
        Returns `passed` so callers can write: if not ctx.gate("X", condition): ...
        """
        self.gates.append({
            "gate":   name,
            "result": "PASS" if passed else "FAIL",
            "reason": reason,
        })
        return passed

    def first_fail(self) -> dict | None:
        """Return the first failing gate entry, or None if all passed."""
        for g in self.gates:
            if g["result"] == "FAIL":
                return g
        return None

    def all_passed(self) -> bool:
        return all(g["result"] == "PASS" for g in self.gates)

    def summary(self) -> str:
        """One-line outcome: 'TRADE_OPENED' or 'BLOCKED@<gate>: <reason>'."""
        fail = self.first_fail()
        if fail:
            r = f": {fail['reason']}" if fail["reason"] else ""
            return f"BLOCKED@{fail['gate']}{r}"
        return "TRADE_OPENED"


def log_candidate_reject(
    symbol: str, strategy: str, direction: str, tf: int,
    bar_ts_str: str, filter_name: str, detail: str = "",
) -> None:
    """
    Log a scanner candidate that was rejected by a named filter.

    Call this ONLY after the key entry precondition has already passed:
      - signal_engine.py: after price-in-zone check passes (FVG retest confirmed)
      - s6_strategy.py:   after CISD is found (LTF setup is complete)

    Rejection filters worth logging:
      PD_ZONE      — bull needs discount zone / bear needs premium zone
      CHOCH_BIAS   — CHoCH direction disagrees with trade direction
      HTF_OB       — HTF order block direction misaligned (S5)
      HTF_DELIVERY — HTF delivery direction misaligned (S6)
    """
    try:
        import event_logger as _el
        _el.candidate_reject(
            symbol=symbol, strategy=strategy, direction=direction,
            tf=tf, bar_ts_str=bar_ts_str,
            filter_name=filter_name, detail=detail,
        )
    except Exception:
        pass  # never let logging crash the scanner
