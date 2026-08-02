"""
Paper Tracker
=============
Tracks virtual positions from live signals.
Monitors SL / TP / EOD exits each minute.
Logs all closed trades and computes running P&L.
"""

import json
import csv
import os
import threading
from datetime import datetime, time as dtime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional

from config import POSITION_SIZE_RS, CHARGES_PCT, SLIPPAGE_PCT, LOG_DIR
from signal_engine import Signal


CHARGE_RS    = POSITION_SIZE_RS * CHARGES_PCT  / 100.0   # Rs per round-trip
SLIP_FACTOR  = SLIPPAGE_PCT / 100.0                        # fraction applied per leg


@dataclass
class Position:
    symbol:      str
    strategy:    str
    direction:   str
    entry_time:  str
    entry_price: float
    sl_price:    float
    tp_price:    float
    entry_tf:    int
    risk_pts:    float
    fvg_top:     float
    fvg_bottom:  float
    pd_zone:     str
    htf_signal:  Optional[str]
    trade_db_id: Optional[int] = None   # SQLite trades.id — set after insert_trade()


@dataclass
class ClosedTrade:
    symbol:       str
    strategy:     str
    direction:    str
    entry_time:   str
    exit_time:    str
    entry_price:  float
    exit_price:   float
    exit_reason:  str   # TP / SL / EOD
    pnl_pts:      float
    pnl_pct:      float
    pnl_rs:       float
    win:          bool
    entry_tf:     int
    pd_zone:      str
    htf_signal:   Optional[str]


class PaperTracker:
    def __init__(self):
        self.open_positions: dict[str, Position] = {}   # symbol -> Position
        self.closed_trades:  list[ClosedTrade]   = []
        self._lock = threading.Lock()   # guards open_position check+insert (race condition fix)
        self._sl_cooldown: dict = {}    # (symbol, strategy) → expiry datetime; set on SL hit
        self._trade_log_path = os.path.join(LOG_DIR, f"paper_trades_{datetime.now().strftime('%Y%m%d')}.csv")
        self._signal_log_path = os.path.join(LOG_DIR, f"signals_{datetime.now().strftime('%Y%m%d')}.jsonl")
        self._write_header()

    def _write_header(self):
        if not os.path.exists(self._trade_log_path):
            with open(self._trade_log_path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow([
                    "symbol","strategy","direction","entry_time","exit_time",
                    "entry_price","exit_price","exit_reason","pnl_pts","pnl_pct",
                    "pnl_rs","win","entry_tf","pd_zone","htf_signal"
                ])

    def log_signal(self, sig: Signal):
        with open(self._signal_log_path, "a") as f:
            f.write(json.dumps({
                "ts": sig.timestamp.isoformat(),
                "symbol": sig.symbol, "strategy": sig.strategy,
                "direction": sig.direction, "entry_price": sig.entry_price,
                "sl": sig.sl_price, "tp": sig.tp_price, "tf": sig.entry_tf,
                "htf": sig.htf_signal,
            }) + "\n")

    def reload_open_positions(self) -> list[str]:
        """
        On startup: reload open positions from SQLite that survived a service restart.

        Called once from app.startup() before the live engine starts.
        Returns list of reloaded symbols so the caller can log/alert.

        SQLite trades table stores all fields needed for SL/TP monitoring.
        Fields not in the table (risk_pts, fvg_top, fvg_bottom) default to 0.0
        — they are informational only and not used by update_positions().
        """
        try:
            import db as _db
            open_trades = _db.get_open_trades()
        except Exception as e:
            print(f"[paper_tracker] reload_open_positions: db read failed — {e}", flush=True)
            return []

        reloaded = []
        for t in open_trades:
            sym = t["symbol"]
            if sym in self.open_positions:
                continue   # already in memory (shouldn't happen on fresh startup)
            try:
                self.open_positions[sym] = Position(
                    symbol      = t["symbol"],
                    strategy    = t["strategy"],
                    direction   = t["direction"],
                    entry_time  = t["entry_time"],
                    entry_price = float(t["entry_price"] or 0),
                    sl_price    = float(t["sl_price"]    or 0),
                    tp_price    = float(t["tp_price"]    or 0),
                    entry_tf    = int(t["entry_tf"]      or 0),
                    risk_pts    = 0.0,          # not persisted — informational only
                    fvg_top     = 0.0,          # not persisted — informational only
                    fvg_bottom  = 0.0,          # not persisted — informational only
                    pd_zone     = t.get("pd_zone")    or "unknown",
                    htf_signal  = t.get("htf_signal"),
                    trade_db_id = t["id"],      # needed for db.close_trade() on exit
                )
                reloaded.append(sym)
                print(
                    f"  ↩ RELOADED {sym} {t['direction'].upper()} "
                    f"entry={t['entry_price']} SL={t['sl_price']} TP={t['tp_price']}",
                    flush=True,
                )
            except Exception as e:
                print(f"[paper_tracker] reload failed for {sym}: {e}", flush=True)

        return reloaded

    def open_position(self, sig: Signal) -> bool:
        """Open a new virtual position from a signal, applying entry slippage.

        Returns True if a new position was opened, False if the symbol already
        had an open position or was blocked by SL cooldown.  Callers MUST check
        the return value before doing any follow-on work (DB insert, logging,
        etc.) — this is what prevents the TOCTOU duplicate-trade bug where
        multiple ThreadPoolExecutor threads pass the app-level POSITION_FREE
        gate before any of them has finished inserting into open_positions.
        """
        # Lock makes the check+insert atomic — prevents race condition when
        # two ThreadPoolExecutor workers process the same symbol concurrently.
        with self._lock:
            if sig.symbol in self.open_positions:
                return False   # already in a trade on this symbol
            # SL cooldown: block re-entry on same (symbol, strategy) for 45 min after a SL hit.
            # Prevents the same CISD event (scan window = 45 min) from reopening after SL.
            sl_key = (sig.symbol, sig.strategy)
            if sl_key in self._sl_cooldown:
                if datetime.now() < self._sl_cooldown[sl_key]:
                    return False   # same setup caused a SL recently — skip re-entry
                del self._sl_cooldown[sl_key]   # expired, clean up
            # Slippage: we pay more on buy, receive less on sell (#44)
            if sig.direction == "bull":
                slipped_entry = round(sig.entry_price * (1 + SLIP_FACTOR), 2)
            else:
                slipped_entry = round(sig.entry_price * (1 - SLIP_FACTOR), 2)
            self.open_positions[sig.symbol] = Position(
                symbol=sig.symbol, strategy=sig.strategy,
                direction=sig.direction,
                entry_time=sig.timestamp.isoformat(),
                entry_price=slipped_entry,          # slipped price stored
                sl_price=sig.sl_price, tp_price=sig.tp_price,
                entry_tf=sig.entry_tf, risk_pts=sig.risk_pts,
                fvg_top=sig.fvg_top, fvg_bottom=sig.fvg_bottom,
                pd_zone=sig.pd_zone, htf_signal=sig.htf_signal,
            )
        # Logging/alerts outside the lock — no shared state, no need for exclusion
        print(f"  📥 OPENED  {sig.summary()} [entry slipped {sig.entry_price}→{slipped_entry}]", flush=True)
        self.log_signal(sig)
        return True

    def update_positions(self, current_prices: dict[str, float], current_time: datetime,
                         force_eod: bool = False):
        """
        Called every minute with latest close prices.
        Checks SL/TP/EOD exits for all open positions.
        force_eod=True: hard-close all positions regardless of time (used by EOD watcher).
        """
        eod = force_eod or (current_time.time() >= dtime(15, 15))
        to_close = []

        for sym, pos in self.open_positions.items():
            price = current_prices.get(sym)
            if price is None:
                continue

            exit_price  = None
            exit_reason = None

            if pos.direction == "bull":
                if price <= pos.sl_price:
                    exit_price, exit_reason = pos.sl_price, "SL"
                elif price >= pos.tp_price:
                    exit_price, exit_reason = pos.tp_price, "TP"
                elif eod:
                    exit_price, exit_reason = price, "EOD"
            else:
                if price >= pos.sl_price:
                    exit_price, exit_reason = pos.sl_price, "SL"
                elif price <= pos.tp_price:
                    exit_price, exit_reason = pos.tp_price, "TP"
                elif eod:
                    exit_price, exit_reason = price, "EOD"

            if exit_price is not None:
                to_close.append((sym, pos, exit_price, exit_reason, current_time))

        for sym, pos, exit_price, exit_reason, ts in to_close:
            self._close(pos, exit_price, exit_reason, ts)
            del self.open_positions[sym]

    def _close(self, pos: Position, exit_price: float, exit_reason: str, ts: datetime):
        # Apply exit slippage: sell lower on LONG exit, buy higher on SHORT exit (#44)
        if pos.direction == "bull":
            slipped_exit = round(exit_price * (1 - SLIP_FACTOR), 2)
            pnl_pts      = slipped_exit - pos.entry_price
        else:
            slipped_exit = round(exit_price * (1 + SLIP_FACTOR), 2)
            pnl_pts      = pos.entry_price - slipped_exit

        exit_price = slipped_exit   # use slipped exit for all reporting
        pnl_pct    = pnl_pts / pos.entry_price * 100
        pnl_rs     = round(pnl_pct / 100 * POSITION_SIZE_RS - CHARGE_RS, 2)
        win        = bool(pnl_pts > 0)   # bool() prevents numpy.bool_ → JSON crash

        trade = ClosedTrade(
            symbol=pos.symbol, strategy=pos.strategy,
            direction=pos.direction,
            entry_time=pos.entry_time,
            exit_time=ts.isoformat(),
            entry_price=pos.entry_price, exit_price=round(exit_price, 2),
            exit_reason=exit_reason,
            pnl_pts=round(pnl_pts, 2), pnl_pct=round(pnl_pct, 4),
            pnl_rs=pnl_rs, win=win,
            entry_tf=pos.entry_tf, pd_zone=pos.pd_zone,
            htf_signal=pos.htf_signal,
        )
        self.closed_trades.append(trade)

        emoji = "✅" if win else "❌"
        pnl_str = f"+₹{pnl_rs:,.0f}" if pnl_rs >= 0 else f"-₹{abs(pnl_rs):,.0f}"
        print(
            f"  {emoji} CLOSED {pos.symbol} {pos.direction.upper()} "
            f"| {exit_reason} @ ₹{exit_price:.2f} | {pnl_str}",
            flush=True
        )
        # SL cooldown: block same (symbol, strategy) for 45 min after SL hit.
        # 45 min = CISD_SCAN_WINDOW (15 bars × 3 min) — ensures the failed CISD
        # drops out of s6_strategy's scan window before re-entry is allowed.
        # Also prevents S4A/S5 from re-entering the same failed zone immediately.
        if exit_reason == "SL":
            sl_key = (pos.symbol, pos.strategy)
            self._sl_cooldown[sl_key] = datetime.now() + timedelta(minutes=45)
            print(f"  🚫 SL COOLDOWN {pos.symbol} [{pos.strategy}] — re-entry blocked 45 min", flush=True)
        self._append_trade(trade)

        # ── Persist close to SQLite ───────────────────────────────────────────
        if pos.trade_db_id:
            try:
                import db as _db
                _db.close_trade(pos.trade_db_id, {
                    "exit_time":   ts.isoformat(),
                    "exit_price":  round(exit_price, 2),
                    "exit_reason": exit_reason,
                    "pnl_pts":     round(pnl_pts,  2),
                    "pnl_pct":     round(pnl_pct,  4),
                    "pnl_rs":      pnl_rs,
                    "win":         int(win),
                })
            except Exception as _e:
                print(f"[paper_tracker] db.close_trade failed: {_e}", flush=True)

        # ── Structured event log ──────────────────────────────────────────────
        try:
            import event_logger as _el
            _el.position_close(
                pos.symbol, pos.direction,
                pos.entry_price, exit_price,
                exit_reason, pnl_rs,
            )
        except Exception as _e:
            print(f"[paper_tracker] elog.position_close failed: {_e}", flush=True)

        # ── Telegram notification ─────────────────────────────────────────────
        try:
            import telegram_notifier as _tg
            _tg.position_close(
                pos.symbol, pos.direction,
                exit_price, exit_reason, pnl_rs,
            )
        except Exception as _e:
            print(f"[paper_tracker] tg.position_close failed: {_e}", flush=True)

    def _append_trade(self, t: ClosedTrade):
        with open(self._trade_log_path, "a", newline="") as f:
            csv.writer(f).writerow([
                t.symbol, t.strategy, t.direction,
                t.entry_time, t.exit_time,
                t.entry_price, t.exit_price, t.exit_reason,
                t.pnl_pts, t.pnl_pct, t.pnl_rs, t.win,
                t.entry_tf, t.pd_zone, t.htf_signal,
            ])

    # ── Stats ─────────────────────────────────────────────────────────────────

    def daily_stats(self) -> dict:
        trades = self.closed_trades
        if not trades:
            return {"trades": 0, "wins": 0, "wr": 0, "net_pnl": 0, "open": len(self.open_positions)}
        wins   = sum(1 for t in trades if t.win)
        net    = sum(t.pnl_rs for t in trades)
        return {
            "trades":   len(trades),
            "wins":     wins,
            "wr":       round(wins / len(trades) * 100, 1),
            "net_pnl":  round(net, 0),
            "open":     len(self.open_positions),
            "open_syms": list(self.open_positions.keys()),
        }

    def state_snapshot(self) -> dict:
        """Full state for dashboard refresh."""
        return {
            "stats":    self.daily_stats(),
            "open":     [asdict(p) for p in self.open_positions.values()],
            "closed":   [asdict(t) for t in self.closed_trades[-50:]],  # last 50
        }
