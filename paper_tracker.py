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
from datetime import datetime, time as dtime
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

    def open_position(self, sig: Signal):
        """Open a new virtual position from a signal, applying entry slippage."""
        if sig.symbol in self.open_positions:
            return   # already in a trade on this symbol
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
        print(f"  📥 OPENED  {sig.summary()} [entry slipped {sig.entry_price}→{slipped_entry}]", flush=True)
        self.log_signal(sig)

    def update_positions(self, current_prices: dict[str, float], current_time: datetime):
        """
        Called every minute with latest close prices.
        Checks SL/TP/EOD exits for all open positions.
        """
        eod = current_time.time() >= dtime(15, 15)
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
        win        = pnl_pts > 0

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
        self._append_trade(trade)

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
