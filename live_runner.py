"""
Live Runner
===========
Main entry point. Runs in two modes:

  MOCK_MODE=True  → replays historical parquet data bar-by-bar (for testing)
  MOCK_MODE=False → connects to Kite WebSocket for real-time 1min bars

Usage:
    python live_runner.py            # uses MOCK_MODE from config.py
    python live_runner.py --mock     # force mock mode
    python live_runner.py --live     # force live mode (needs Kite API)
    python live_runner.py --date 2026-01-15  # mock: replay specific date
"""

import sys
import os
import time
import argparse
import json
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict

import pandas as pd
import numpy as np

# Add parent to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg
from signal_engine import scan_symbol
from paper_tracker import PaperTracker

# ── Rolling bar window per symbol ─────────────────────────────────────────────

class BarWindow:
    """Maintains a rolling window of 1min OHLCV bars per symbol."""
    def __init__(self, max_bars: int = cfg.WARMUP_BARS):
        self.max_bars = max_bars
        self._data: dict[str, pd.DataFrame] = {}

    def seed(self, symbol: str, df: pd.DataFrame):
        """Pre-load historical bars for warmup."""
        self._data[symbol] = df.tail(self.max_bars).reset_index(drop=True)

    def push(self, symbol: str, bar: dict):
        """Add a new completed 1min bar."""
        new_row = pd.DataFrame([bar])
        if symbol not in self._data:
            self._data[symbol] = new_row
        else:
            self._data[symbol] = pd.concat(
                [self._data[symbol], new_row], ignore_index=True
            ).tail(self.max_bars).reset_index(drop=True)

    def get(self, symbol: str) -> pd.DataFrame | None:
        return self._data.get(symbol)


# ── Warmup: load historical data for all symbols ──────────────────────────────

def warmup(bar_window: BarWindow, all_symbols: list[str]):
    """Seed each symbol with the last WARMUP_BARS of historical 1min data."""
    print(f"Warming up {len(all_symbols)} symbols with {cfg.WARMUP_BARS} bars each...", flush=True)
    import smc_backtest as bt
    for sym in all_symbols:
        try:
            df = bt.load_1min(sym)
            if df is not None and len(df) > 0:
                bar_window.seed(sym, df)
                print(f"  ✓ {sym}: {len(df)} bars loaded", flush=True)
            else:
                print(f"  ✗ {sym}: no data", flush=True)
        except Exception as e:
            print(f"  ✗ {sym}: {e}", flush=True)


# ── State file for dashboard ──────────────────────────────────────────────────

STATE_FILE = os.path.join(cfg.LOG_DIR, "live_state.json")

def write_state(tracker: PaperTracker, signals_today: list):
    snap = tracker.state_snapshot()
    snap["signals_today"] = signals_today[-20:]   # last 20 signals
    snap["last_updated"] = datetime.now().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(snap, f, default=str)


# ── Signal deduplication ──────────────────────────────────────────────────────

class SignalDedup:
    """Prevent re-alerting the same signal within a 5-minute window."""
    def __init__(self, cooldown_mins: int = 5):
        self._seen: dict[str, datetime] = {}
        self.cooldown = timedelta(minutes=cooldown_mins)

    def is_new(self, sym: str, direction: str, tf: int, ts: datetime) -> bool:
        key = f"{sym}:{direction}:{tf}"
        if key in self._seen:
            if ts - self._seen[key] < self.cooldown:
                return False
        self._seen[key] = ts
        return True


# ── Mock mode: replay historical bars ────────────────────────────────────────

def run_mock(tracker: PaperTracker, bar_window: BarWindow,
             all_symbols: list[str], strategy_cfgs: list[dict],
             replay_date: str | None = None):
    """
    Replay historical data bar-by-bar to simulate live trading.
    Useful for testing the signal engine without a live API.
    """
    import smc_backtest as bt
    dedup = SignalDedup()
    signals_today = []

    # Pick replay date
    if replay_date:
        target = pd.Timestamp(replay_date)
    else:
        # Use yesterday by default (last trading day in parquet)
        target = pd.Timestamp(date.today() - timedelta(days=1))

    print(f"\n{'='*60}", flush=True)
    print(f"MOCK MODE — Replaying {target.date()}", flush=True)
    print(f"Symbols: {all_symbols}", flush=True)
    print(f"{'='*60}\n", flush=True)

    # Load full 1min data for all symbols, filter to target date
    sym_day_data = {}
    for sym in all_symbols:
        try:
            df = bt.load_1min(sym)
            day_df = df[df["ts"].dt.date == target.date()].reset_index(drop=True)
            if len(day_df) > 0:
                sym_day_data[sym] = day_df
        except Exception:
            pass

    if not sym_day_data:
        print("No data for target date. Try a different --date", flush=True)
        return

    # Get all unique bar timestamps (union across symbols), in order
    all_ts = sorted(set(
        ts for df in sym_day_data.values() for ts in df["ts"].tolist()
    ))

    print(f"Replaying {len(all_ts)} bars from {all_ts[0]} to {all_ts[-1]}\n", flush=True)

    for bar_ts in all_ts:
        now = pd.Timestamp(bar_ts)
        print(f"  [{now.strftime('%H:%M')}] ", end="", flush=True)

        current_prices = {}

        for sym in all_symbols:
            if sym not in sym_day_data:
                continue
            df = sym_day_data[sym]
            bar_row = df[df["ts"] == now]
            if bar_row.empty:
                continue

            bar = bar_row.iloc[0].to_dict()
            current_prices[sym] = float(bar["close"])
            bar_window.push(sym, bar)

        # Update open positions
        tracker.update_positions(current_prices, now.to_pydatetime())

        # Scan for new signals
        for scfg in strategy_cfgs:
            for sym in scfg["symbols"]:
                if sym not in all_symbols:
                    continue
                df_1min = bar_window.get(sym)
                if df_1min is None or len(df_1min) < 50:
                    continue
                try:
                    new_sigs = scan_symbol(sym, df_1min, scfg, cfg.SESSION_WINDOWS)
                except Exception:
                    continue
                for sig in new_sigs:
                    if dedup.is_new(sym, sig.direction, sig.entry_tf, now.to_pydatetime()):
                        signals_today.append({
                            "ts": sig.timestamp.isoformat(),
                            "symbol": sym, "direction": sig.direction,
                            "entry": sig.entry_price, "sl": sig.sl_price, "tp": sig.tp_price,
                            "strategy": sig.strategy, "tf": sig.entry_tf,
                        })
                        tracker.open_position(sig)
                        print(f"\n  🔔 SIGNAL: {sig.summary()}", flush=True)

        write_state(tracker, signals_today)
        print("", flush=True)

        # Simulated 1-minute pace (remove or reduce for speed)
        # time.sleep(0.05)

    # Print daily summary
    stats = tracker.daily_stats()
    print(f"\n{'='*60}", flush=True)
    print(f"DAILY SUMMARY — {target.date()}", flush=True)
    print(f"  Trades : {stats['trades']}", flush=True)
    print(f"  Wins   : {stats['wins']} ({stats['wr']}%)", flush=True)
    net = stats['net_pnl']
    print(f"  Net P&L: {'+'if net>=0 else ''}₹{net:,.0f}", flush=True)
    print(f"  Open   : {stats['open']} ({stats.get('open_syms',[])})", flush=True)
    print(f"{'='*60}\n", flush=True)


# ── Live mode: Kite WebSocket ─────────────────────────────────────────────────

def run_live(tracker: PaperTracker, bar_window: BarWindow,
             all_symbols: list[str], strategy_cfgs: list[dict]):
    """
    Live mode: subscribe to Kite WebSocket, aggregate ticks to 1min bars,
    then run the same signal engine as mock mode.
    """
    try:
        from kiteconnect import KiteConnect, KiteTicker
    except ImportError:
        print("ERROR: kiteconnect not installed. Run: pip install kiteconnect", flush=True)
        return

    kite = KiteConnect(api_key=cfg.API_KEY)
    kite.set_access_token(cfg.ACCESS_TOKEN)

    # Get instrument tokens for symbols
    instruments = kite.instruments("NSE")
    inst_map = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}
    tokens = [inst_map[s] for s in all_symbols if s in inst_map]
    token_to_sym = {inst_map[s]: s for s in all_symbols if s in inst_map}

    # 1min bar accumulators
    bar_accum: dict[int, dict] = {}   # token -> current incomplete bar

    dedup = SignalDedup()
    signals_today = []

    def on_ticks(ws, ticks):
        nonlocal signals_today
        for tick in ticks:
            token = tick["instrument_token"]
            sym   = token_to_sym.get(token)
            if sym is None:
                continue

            ltp = tick.get("last_price", 0)
            ts  = pd.Timestamp(tick.get("timestamp", datetime.now()))
            minute_key = ts.floor("1min")

            if token not in bar_accum or bar_accum[token]["ts"] != minute_key:
                # New minute — flush completed bar
                if token in bar_accum:
                    completed = bar_accum[token]
                    bar_window.push(sym, completed)
                    current_prices = {sym: completed["close"]}
                    tracker.update_positions(current_prices, minute_key.to_pydatetime())

                    # Scan for signals
                    df_1min = bar_window.get(sym)
                    if df_1min is not None and len(df_1min) >= 50:
                        for scfg in strategy_cfgs:
                            if sym not in scfg["symbols"]:
                                continue
                            try:
                                for sig in scan_symbol(sym, df_1min, scfg, cfg.SESSION_WINDOWS):
                                    if dedup.is_new(sym, sig.direction, sig.entry_tf, minute_key.to_pydatetime()):
                                        signals_today.append({
                                            "ts": sig.timestamp.isoformat(),
                                            "symbol": sym, "direction": sig.direction,
                                            "entry": sig.entry_price, "sl": sig.sl_price,
                                            "tp": sig.tp_price, "strategy": sig.strategy,
                                            "tf": sig.entry_tf,
                                        })
                                        tracker.open_position(sig)
                                        print(f"\n🔔 {sig.summary()}", flush=True)
                            except Exception:
                                pass
                    write_state(tracker, signals_today)

                bar_accum[token] = {
                    "ts": minute_key, "open": ltp, "high": ltp,
                    "low": ltp, "close": ltp, "volume": 0,
                }
            else:
                b = bar_accum[token]
                b["high"]   = max(b["high"], ltp)
                b["low"]    = min(b["low"],  ltp)
                b["close"]  = ltp
                b["volume"] += tick.get("volume", 0)

    def on_connect(ws, resp):
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_LTP, tokens)
        print(f"Connected. Subscribed to {len(tokens)} instruments.", flush=True)

    def on_error(ws, code, reason):
        print(f"WebSocket error {code}: {reason}", flush=True)

    def on_close(ws, code, reason):
        print(f"WebSocket closed {code}: {reason}", flush=True)

    ticker = KiteTicker(cfg.API_KEY, cfg.ACCESS_TOKEN)
    ticker.on_ticks   = on_ticks
    ticker.on_connect = on_connect
    ticker.on_error   = on_error
    ticker.on_close   = on_close

    print("Starting Kite WebSocket...", flush=True)
    ticker.connect(threaded=False)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SMC Live Paper Trader")
    parser.add_argument("--mock",  action="store_true", help="Force mock mode")
    parser.add_argument("--live",  action="store_true", help="Force live mode")
    parser.add_argument("--date",  default=None, help="Mock replay date (YYYY-MM-DD)")
    args = parser.parse_args()

    use_mock = cfg.MOCK_MODE
    if args.mock: use_mock = True
    if args.live: use_mock = False

    # Collect all unique symbols and build strategy config list
    all_symbols = []
    strategy_cfgs = []
    for name, scfg in cfg.STRATEGIES.items():
        if not scfg.get("enabled", True):
            continue
        scfg = dict(scfg, name=name)
        strategy_cfgs.append(scfg)
        for s in scfg["symbols"]:
            if s not in all_symbols:
                all_symbols.append(s)

    bar_window = BarWindow()
    tracker    = PaperTracker()

    # Warmup
    warmup(bar_window, all_symbols)

    if use_mock:
        run_mock(tracker, bar_window, all_symbols, strategy_cfgs, replay_date=args.date)
    else:
        run_live(tracker, bar_window, all_symbols, strategy_cfgs)


if __name__ == "__main__":
    main()
