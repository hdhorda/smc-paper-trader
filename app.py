"""
app.py — SMC Paper Trading | Flask server
=========================================
PAPER TRADING ONLY — no real orders are ever placed.
KiteConnect.place_order is blocked at startup (hard guardrail).

Endpoints:
  GET  /               -> live dashboard HTML
  GET  /api/state      -> current snapshot (JSON)
  GET  /api/trades     -> all closed trades (JSON)
  GET  /api/signals    -> recent signals (JSON)
  GET  /api/stats      -> daily + lifetime P&L stats (JSON)
  GET  /api/logs       -> recent event log entries (JSON)
  GET  /api/health     -> uptime check (JSON)
  GET  /auth           -> Kite login redirect (refresh token daily before 9:15)
  GET  /auth/callback  -> exchange request_token -> access_token
"""

import os
import sys
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, time as dtime, timedelta

from flask import Flask, jsonify, request, redirect, send_from_directory
from dotenv import load_dotenv

load_dotenv()

# Market calendar — single source of truth for all session timing
from market_calendar import calendar as mkt

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent))

import db
import event_logger as elog
import telegram_notifier as tg
from signal_engine import scan_symbol
from paper_tracker import PaperTracker
from live_runner import BarWindow

# ── PAPER TRADING GUARDRAIL ────────────────────────────────────────────────────
# Monkey-patch KiteConnect order methods before anything else loads.
# Even if someone accidentally adds order code, it will raise at runtime.
try:
    from kiteconnect import KiteConnect as _KC
    def _blocked(self, *args, **kwargs):
        raise RuntimeError(
            "PAPER_ONLY: KiteConnect.place_order is disabled. "
            "This system is paper trading only. No real orders will be placed."
        )
    _KC.place_order  = _blocked
    _KC.modify_order = _blocked
    _KC.cancel_order = _blocked
except ImportError:
    pass   # kiteconnect not installed (mock mode); fine

# ── Strategy config ────────────────────────────────────────────────────────────
STRATEGIES = {
    "S4A": {
        "enabled": True,
        "name": "S4A",
        "description": "FVG + LiqGrab + CHoCH + PD + Session Filter",
        "timeframes": [3, 5, 15],
        "htf_signal": None,
        "symbols": [
            "ABCAPITAL","APLAPOLLO","PFC","RELIANCE","BOSCHLTD","JSWSTEEL",
            "IEX","OIL","LUPIN","GODREJCP","PAGEIND","LODHA","ALKEM",
            "DIXON","JUBLFOOD","ETERNAL","GMRAIRPORT","DABUR","BAJAJFINSV","FORCEMOT",
        ],
    },
    "S5_OB60_5": {
        "enabled": True,
        "name": "S5_OB60_5",
        "description": "1H Order Block -> 5min FVG entry",
        "timeframes": [5],
        "htf_signal": "ob",
        "htf_tf": 60,
        "symbols": ["TRENT","AMBER","SAIL","CHOLAFIN","BANKBARODA","PGEL","BIOCON"],
    },
}

SESSION_WINDOWS = [("09:15", "11:30"), ("13:30", "15:15")]
MOCK_MODE = os.environ.get("MOCK_MODE", "true").lower() == "true"

# ── Risk guardrails (env-configurable) ────────────────────────────────────────
MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "15"))   # #42 cap
DAILY_LOSS_CAP     = int(os.environ.get("DAILY_LOSS_CAP",     "50000")) # #43 circuit breaker ₹
SCAN_WORKERS       = int(os.environ.get("SCAN_WORKERS",        "4"))    # #35 parallel scan threads

# ── Global state ───────────────────────────────────────────────────────────────
bar_window     = BarWindow(max_bars=500)
tracker        = PaperTracker()
_started       = False
_ws_active     = False
_token_valid   = True
_dedup_seen: dict = {}
_bar_idx: dict = {}              # symbol -> bar counter for signal expiry
_daily_loss_cap_hit: bool = False  # #43: set True when day P&L < -DAILY_LOSS_CAP

# Tick recorder — lazy init when live engine starts (#47)
_tick_recorder = None

# DataGuardian (imported lazily to avoid circular import issues)
_guardian = None
def _get_guardian():
    global _guardian
    if _guardian is None:
        from data_guardian import DataGuardian
        _guardian = DataGuardian()
    return _guardian

# ── Flask ──────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=str(BASE_DIR))
db.init_db()

# ── Helpers ────────────────────────────────────────────────────────────────────

def is_market_open() -> bool:
    return mkt.is_market_open()


def in_cooldown(sym: str, direction: str, tf: int) -> bool:
    key = f"{sym}:{direction}:{tf}"
    if key in _dedup_seen:
        if datetime.now() - _dedup_seen[key] < timedelta(minutes=5):
            return True
    _dedup_seen[key] = datetime.now()
    return False


# ── Signal processing ──────────────────────────────────────────────────────────

_scan_lag_ms: list[float] = []   # module-level so /api/stats can read it
_scan_executor = None            # module-level so on_ticks (in _build_ticker) can access it

def _safe_process_bar(sym: str, bar: dict, strategy_cfgs: list, kite, t0: float):
    """Wrapper for parallel executor — catches exceptions, records scan lag."""
    try:
        process_bar(sym, bar, strategy_cfgs, kite)
    except Exception as exc:
        elog.error("ERROR", f"process_bar exception for {sym}: {exc}", exc=exc,
                   data={"symbol": sym})
    finally:
        lag = (time.monotonic() - t0) * 1000
        _scan_lag_ms.append(lag)
        if len(_scan_lag_ms) > 500:
            _scan_lag_ms[:] = _scan_lag_ms[-500:]


def process_bar(sym: str, bar: dict, strategy_cfgs: list, kite=None):
    import pandas as pd
    bar_window.push(sym, bar)
    guardian = _get_guardian()

    # Track bar index for signal expiry
    _bar_idx[sym] = _bar_idx.get(sym, 0) + 1
    bar_ts = bar.get("ts") or pd.Timestamp(datetime.now()).floor("1min")
    guardian.on_bar_close(sym, bar_ts, bar=bar)   # pass bar for circuit breaker check (#48)

    # Gap check (backfill if needed)
    if kite:
        guardian.check_gap(sym, kite, bar_window, tracker, db)

    # Warmup guard: don't scan until enough bars
    if not guardian.can_scan(sym, bar_window):
        return

    # Data quality guard: skip if stale or thin ticks
    if not guardian.data_quality_ok(sym):
        return

    # Entry gate: no new entries after 15:00 or on non-trading days
    if not mkt.is_entry_allowed():
        return

    # Daily loss cap circuit breaker (#43)
    if _daily_loss_cap_hit:
        return

    df_1min = bar_window.get(sym)
    if df_1min is None or len(df_1min) < 50:
        return

    # ── Collect ALL signals from all applicable strategies (#45 conflict check) ──
    all_signals = []
    for scfg in strategy_cfgs:
        if sym not in scfg["symbols"]:
            continue
        try:
            sigs = scan_symbol(sym, df_1min, scfg, SESSION_WINDOWS)
            all_signals.extend(sigs)
        except Exception as exc:
            elog.error("ERROR", f"scan_symbol failed for {sym}", exc=exc,
                       data={"symbol": sym, "strategy": scfg["name"]})

    if not all_signals:
        return

    # Direction conflict: two strategies disagree → skip all (#45)
    bulls = [s for s in all_signals if s.direction == "bull"]
    bears = [s for s in all_signals if s.direction == "bear"]
    if bulls and bears:
        bull_strats = [s.strategy for s in bulls]
        bear_strats = [s.strategy for s in bears]
        elog.warn("SIGNAL_CONFLICT",
                  f"{sym}: {len(bulls)}×LONG vs {len(bears)}×SHORT across strategies — skipping all",
                  data={"symbol": sym, "bull_strategies": bull_strats, "bear_strategies": bear_strats})
        tg.signal_conflict(sym, bull_strats, bear_strats)
        return

    for sig in all_signals:
        if in_cooldown(sym, sig.direction, sig.entry_tf):
            continue

        # Signal expiry check (price drift or too many bars elapsed)
        valid, reason = guardian.is_signal_valid(sym, sig.entry_price, _bar_idx[sym])
        if not valid:
            continue   # already logged by guardian

        db.insert_signal({
            "fired_at": sig.timestamp.isoformat(),
            "symbol": sym, "strategy": sig.strategy,
            "direction": sig.direction,
            "entry_price": sig.entry_price,
            "sl_price": sig.sl_price, "tp_price": sig.tp_price,
            "entry_tf": sig.entry_tf, "htf_signal": sig.htf_signal,
            "fvg_top": sig.fvg_top, "fvg_bottom": sig.fvg_bottom,
            "pd_zone": sig.pd_zone,
        })
        elog.signal_fired(sym, sig.strategy, sig.direction, sig.entry_tf, sig.entry_price)
        tg.signal_fired(sym, sig.direction, sig.strategy, sig.entry_price, sig.sl_price, sig.tp_price, sig.entry_tf)
        guardian.register_signal(sig, _bar_idx[sym])

        if sym not in tracker.open_positions:
            # Max concurrent positions cap (#42)
            if len(tracker.open_positions) >= MAX_OPEN_POSITIONS:
                elog.warn("POSITION_CAP_HIT",
                          f"Cap {MAX_OPEN_POSITIONS} reached — queuing {sym}",
                          data={"symbol": sym, "cap": MAX_OPEN_POSITIONS,
                                "open": len(tracker.open_positions)})
                tg.position_cap_hit(sym, MAX_OPEN_POSITIONS, len(tracker.open_positions))
                continue
            tracker.open_position(sig)
            guardian.consume_signal(sym)
            entry_p = tracker.open_positions[sym].entry_price  # slipped price
            db.insert_trade({
                "symbol": sym, "strategy": sig.strategy,
                "direction": sig.direction,
                "entry_time": sig.timestamp.isoformat(),
                "entry_price": entry_p,
                "sl_price": sig.sl_price, "tp_price": sig.tp_price,
                "entry_tf": sig.entry_tf,
                "pd_zone": sig.pd_zone, "htf_signal": sig.htf_signal,
            })
            elog.position_open(sym, sig.direction, entry_p, sig.sl_price, sig.tp_price)
            tg.position_open(sym, sig.direction, entry_p, sig.sl_price, sig.tp_price, sig.strategy)


# ── Auto-healing WebSocket engine ──────────────────────────────────────────────

def _persist_token(access_token: str):
    """Write KITE_ACCESS_TOKEN into .env so it survives service restarts."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    try:
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                lines = f.readlines()
            with open(env_path, "w") as f:
                replaced = False
                for line in lines:
                    if line.startswith("KITE_ACCESS_TOKEN="):
                        f.write(f"KITE_ACCESS_TOKEN={access_token}\n")
                        replaced = True
                    else:
                        f.write(line)
                if not replaced:
                    f.write(f"KITE_ACCESS_TOKEN={access_token}\n")
    except Exception as e:
        elog.error("ERROR", f"Failed to persist token to .env: {e}")


def _build_ticker(kite, tokens, token_sym, bar_accum, strategy_cfgs):
    from kiteconnect import KiteTicker
    global _ws_active, _token_valid

    def on_ticks(ws, ticks):
        if not mkt.is_market_open() or not _token_valid:
            return
        import pandas as pd
        now = datetime.now()
        current_prices = {}

        for tick in ticks:
            tok = tick["instrument_token"]
            sym = token_sym.get(tok)
            if not sym:
                continue
            ltp = tick.get("last_price", 0)
            ts  = pd.Timestamp(tick.get("timestamp", now))

            # Record tick to parquet (#47)
            if _tick_recorder is not None:
                _tick_recorder.record(sym, ltp, tick.get("volume_traded", 0), now)

            # Update data guardian tick counter (required for data_quality_ok check)
            _get_guardian().on_tick(sym, ltp, ts)

            mk  = ts.floor("1min")

            if tok not in bar_accum or bar_accum[tok]["ts"] != mk:
                if tok in bar_accum:
                    completed = bar_accum[tok]
                    current_prices[sym] = completed["close"]
                    # Submit to parallel worker pool (#35)
                    _t0 = time.monotonic()
                    _scan_executor.submit(
                        _safe_process_bar, sym, completed, strategy_cfgs, kite, _t0
                    )
                bar_accum[tok] = {
                    "ts": mk, "open": ltp, "high": ltp,
                    "low": ltp, "close": ltp, "volume": 0,
                }
            else:
                b = bar_accum[tok]
                b["high"]  = max(b["high"], ltp)
                b["low"]   = min(b["low"],  ltp)
                b["close"] = ltp
                b["volume"] += tick.get("volume", 0)

        if current_prices:
            tracker.update_positions(current_prices, now)

    def on_connect(ws, _):
        global _ws_active
        _ws_active = True
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_LTP, tokens)
        elog.ws_connect(len(tokens))

    def on_error(ws, code, reason):
        global _token_valid
        elog.ws_error(code, reason)
        if code == 403 or "token" in str(reason).lower():
            _token_valid = False
            elog.token_expired()
            tg.token_expired()

    def on_close(ws, code, reason):
        global _ws_active
        _ws_active = False
        elog.ws_disconnect(code, reason)
        tg.ws_disconnect(code, reason)

    # reconnect=False — we manage reconnects ourselves; prevents double-reconnect storm
    ticker = KiteTicker(os.environ["KITE_API_KEY"], os.environ["KITE_ACCESS_TOKEN"], reconnect=False)
    ticker.on_ticks   = on_ticks
    ticker.on_connect = on_connect
    ticker.on_error   = on_error
    ticker.on_close   = on_close
    return ticker


def start_live_engine():
    """
    Main live engine loop with auto-heal:
      - Warmup retries (3 attempts, 10/20s backoff)
      - WebSocket auto-reconnect (5->10->20->40->60s backoff)
      - Heartbeat every 5 min during market hours
      - Pauses gracefully when token invalid, resumes after /auth refresh
    """
    global _token_valid

    from kite_warmer import get_kite_client, warmup_from_kite
    from tick_recorder import TickRecorder
    global _tick_recorder
    _tick_recorder = TickRecorder()

    kite = get_kite_client()
    all_syms      = list({s for cfg in STRATEGIES.values() for s in cfg["symbols"]})
    strategy_cfgs = [v for v in STRATEGIES.values() if v.get("enabled")]

    elog.warmup_start(all_syms)
    warmup_results = warmup_from_kite(kite, all_syms, bar_window)
    elog.warmup_done(warmup_results)

    try:
        instruments = kite.instruments("NSE")
        inst_map  = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}
        tokens    = [inst_map[s] for s in all_syms if s in inst_map]
        token_sym = {inst_map[s]: s for s in all_syms if s in inst_map}
    except Exception as exc:
        elog.error("ERROR", "Failed to fetch instrument list", exc=exc)
        return

    bar_accum: dict = {}

    # Parallel scan executor (#35) — 4 workers, one per logical core on Oracle Free
    global _scan_executor
    _scan_executor = ThreadPoolExecutor(max_workers=SCAN_WORKERS,
                                        thread_name_prefix="scan-worker")

    # Heartbeat thread — every 5 min; also checks daily loss cap (#43)
    def _heartbeat():
        global _daily_loss_cap_hit
        while True:
            time.sleep(300)
            if mkt.is_market_open():
                daily    = db.get_daily_stats()
                net_pnl  = daily.get("net_pnl", 0.0)
                elog.heartbeat(
                    open_positions=len(tracker.open_positions),
                    closed_today=daily.get("trades", 0),
                    net_pnl_today=net_pnl,
                )
                # Daily loss cap circuit breaker (#43)
                if not _daily_loss_cap_hit and net_pnl < -DAILY_LOSS_CAP:
                    _daily_loss_cap_hit = True
                    elog.warn("DAILY_LOSS_CAP_HIT",
                              f"Day P&L ₹{net_pnl:+,.0f} crossed -₹{DAILY_LOSS_CAP:,} — no new entries today",
                              data={"net_pnl": net_pnl, "cap": DAILY_LOSS_CAP})
                    tg.daily_loss_cap_hit(net_pnl, DAILY_LOSS_CAP)
    threading.Thread(target=_heartbeat, daemon=True).start()

    # Daily reset watcher — clears dedup cache and loss cap flag each morning (#46)
    def _daily_reset_watcher():
        global _dedup_seen, _daily_loss_cap_hit
        last_reset_date = ""
        while True:
            time.sleep(30)
            today    = datetime.now().strftime("%Y-%m-%d")
            now_time = datetime.now().time()
            if mkt.is_trading_day() and today != last_reset_date and now_time >= dtime(9, 15):
                _dedup_seen.clear()
                _daily_loss_cap_hit = False
                last_reset_date = today
                elog.info("DAILY_RESET",
                          f"Dedup cache cleared + loss cap reset for {today} at 09:15")
    threading.Thread(target=_daily_reset_watcher, daemon=True).start()

    # EOD hard-close thread — fires at 15:00, closes ALL open positions
    _eod_done_today: set = set()
    def _eod_watcher():
        while True:
            time.sleep(30)   # check every 30 seconds
            today = datetime.now().strftime("%Y-%m-%d")
            if mkt.is_eod_exit_window() and today not in _eod_done_today:
                _eod_done_today.add(today)
                open_syms = list(tracker.open_positions.keys())
                if not open_syms:
                    elog.market_close(0, 0.0)
                    continue
                elog.info("EOD_EXIT", f"Hard close triggered at 15:00 — {len(open_syms)} open positions")
                current_prices = {}
                # Use last known close price from bar_window
                for sym in open_syms:
                    df = bar_window.get(sym)
                    if df is not None and len(df) > 0:
                        current_prices[sym] = float(df.iloc[-1]["close"])
                closed = tracker.update_positions(current_prices, datetime.now(), force_eod=True)
                daily  = db.get_daily_stats()
                elog.market_close(
                    closed_positions=len(open_syms),
                    net_pnl=daily.get("net_pnl", 0.0),
                )
                tg.eod_summary(
                    trades=daily.get("trades", 0),
                    wins=daily.get("wins", 0),
                    net_pnl=daily.get("net_pnl", 0.0),
                    open_closed=len(open_syms),
                )

    # Non-trading day sleep thread — skips weekends/holidays cleanly
    def _calendar_watcher():
        while True:
            time.sleep(60)
            if not mkt.is_trading_day():
                secs = mkt.seconds_to_open()
                elog.info("CALENDAR",
                          f"Non-trading day — sleeping until next open "
                          f"({mkt.next_trading_day()} 09:15, ~{secs//3600}h away)")
                time.sleep(min(secs, 3600))   # wake up at most every hour to re-check

    threading.Thread(target=_eod_watcher,      daemon=True).start()
    threading.Thread(target=_calendar_watcher, daemon=True).start()

    # Weekly consistency checker — runs every Sunday 17:00 IST (#34)
    def _consistency_loop():
        import consistency_checker as cc
        import telegram_notifier as _tg
        while True:
            cc.wait_for_next_sunday_1700()
            report = cc.run(kite=kite)
            elog.info("CONSISTENCY_REPORT",
                      f"Weekly check: {report['overall_match_pct']}% match ({report['status']})",
                      report)
            if report["status"] == "WARN":
                elog.warn("CONSISTENCY_WARN",
                          f"Live signals only {report['overall_match_pct']}% match backtest — check signal engine!",
                          report)
                _tg.error_alert(
                    f"CONSISTENCY WARN: {report['overall_match_pct']}% match "
                    f"(threshold {report['warn_threshold']}%)\n"
                    f"Symbol match: {report['symbol_match_pct']}% | "
                    f"Dir match: {report['direction_match_pct']}%"
                )
    threading.Thread(target=_consistency_loop, daemon=True).start()

    reconnect_attempt = 0
    backoff_delays    = [5, 10, 20, 40, 60]

    elog.info("STARTUP", "WebSocket connection loop starting")

    while True:
        try:
            if not _token_valid:
                elog.warn("TOKEN_EXPIRED", "Engine paused — visit /auth to refresh Kite token")
                time.sleep(60)
                continue

            # Don't hammer Kite outside market hours — prevents IP rate-limiting
            if not mkt.is_market_open():
                time.sleep(60)
                continue

            ticker = _build_ticker(kite, tokens, token_sym, bar_accum, strategy_cfgs)
            ticker.connect(threaded=True)
            reconnect_attempt = 0

            while True:
                time.sleep(30)
                if not _ws_active and is_market_open():
                    elog.warn("WS_DISCONNECT", "WS not active during market — forcing reconnect")
                    try:
                        ticker.close()
                    except Exception:
                        pass
                    break

        except Exception as exc:
            # Kill any lingering ticker before creating a new one
            try:
                ticker.close()
            except Exception:
                pass
            delay = backoff_delays[min(reconnect_attempt, len(backoff_delays) - 1)]
            elog.error("ERROR", f"Engine exception: {exc}", exc=exc)
            elog.ws_reconnect(reconnect_attempt + 1, delay)
            time.sleep(delay)
            reconnect_attempt += 1
            # Re-warm after long outage
            try:
                kite = get_kite_client()
                warmup_from_kite(kite, all_syms, bar_window)
                elog.info("WARMUP_DONE", "Re-warmup after reconnect complete")
            except Exception as exc2:
                elog.error("ERROR", f"Re-warmup failed: {exc2}", exc=exc2)


# ── Auth routes ────────────────────────────────────────────────────────────────

@app.route("/auth")
def auth_redirect():
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=os.environ.get("KITE_API_KEY", ""))
    return redirect(kite.login_url())


@app.route("/auth/callback")
def auth_callback():
    global _token_valid
    from kite_warmer import refresh_access_token
    request_token = request.args.get("request_token")
    if not request_token:
        return "Missing request_token", 400
    try:
        access_token = refresh_access_token(request_token)
        os.environ["KITE_ACCESS_TOKEN"] = access_token
        _token_valid = True
        # Persist token to .env so it survives service restarts
        _persist_token(access_token)
        elog.token_refreshed()
        return f"""<!DOCTYPE html><html><body style="font-family:sans-serif;padding:40px">
        <h2>✅ Access Token Refreshed</h2>
        <p>Token saved to .env and active. Engine resumes automatically at 09:15.</p>
        <code style="background:#f4f4f4;padding:12px;display:block;word-break:break-all;margin:16px 0">
        {access_token}</code>
        <p style="color:#888">Valid until midnight today (Kite resets daily).</p>
        <a href="/">Back to Dashboard</a>
        </body></html>"""
    except Exception as e:
        elog.error("ERROR", f"Token refresh failed: {e}")
        return f"Error: {e}", 500


# ── API routes ─────────────────────────────────────────────────────────────────

@app.route("/api/state")
def api_state():
    snap = tracker.state_snapshot()
    cal  = mkt.status()
    snap["market_open"]       = cal["market_open"]
    snap["entry_allowed"]     = cal["entry_allowed"]
    snap["market_phase"]      = cal["phase"]
    snap["mode"]              = "MOCK" if MOCK_MODE else "LIVE"
    snap["paper_only"]        = True
    snap["ws_active"]         = _ws_active
    snap["token_valid"]       = _token_valid
    snap["last_updated"]      = datetime.now().isoformat()
    snap["strategy_summary"]  = db.get_strategy_summary()
    snap["calendar"]          = cal
    snap["loss_cap_hit"]      = _daily_loss_cap_hit
    snap["max_open_positions"] = MAX_OPEN_POSITIONS
    return jsonify(snap)


@app.route("/api/trades")
def api_trades():
    strategy = request.args.get("strategy")
    limit    = int(request.args.get("limit", 200))
    return jsonify({
        "open":   db.get_open_trades(),
        "closed": db.get_closed_trades(limit=limit, strategy=strategy),
    })


@app.route("/api/signals")
def api_signals():
    limit = int(request.args.get("limit", 50))
    return jsonify(db.get_recent_signals(limit=limit))


@app.route("/api/stats")
def api_stats():
    lag = _scan_lag_ms[-100:] if _scan_lag_ms else []
    # Load latest consistency report from disk if available
    import json as _json
    from pathlib import Path as _Path
    consistency = None
    try:
        p = _Path(os.environ.get("LOG_DIR", "logs")) / "consistency_latest.json"
        if p.exists():
            consistency = _json.loads(p.read_text())
    except Exception:
        pass
    return jsonify({
        "daily":          db.get_daily_stats(request.args.get("date")),
        "by_strategy":    db.get_strategy_summary(),
        "scan_lag_ms": {
            "samples":    len(lag),
            "avg":        round(sum(lag) / len(lag), 1) if lag else 0,
            "max":        round(max(lag), 1) if lag else 0,
            "p95":        round(sorted(lag)[int(len(lag)*0.95)] if len(lag) > 20 else 0, 1),
        },
        "workers":            SCAN_WORKERS,
        "consistency_report": consistency,
    })


@app.route("/api/logs")
def api_logs():
    limit      = int(request.args.get("limit", 100))
    level      = request.args.get("level")
    event_type = request.args.get("event")
    date_str   = request.args.get("date")
    if date_str:
        events = elog.get_from_file(date_str, limit=limit)
    else:
        events = elog.get_recent(limit=limit, level=level, event_type=event_type)
    return jsonify({"events": events, "count": len(events)})


@app.route("/api/health")
def health():
    cal  = mkt.status()
    base = {
        "status":        "ok",
        "time":          datetime.now().isoformat(),
        "mode":          "MOCK" if MOCK_MODE else "LIVE",
        "paper_only":    True,
        "ws_active":     _ws_active,
        "token_valid":   _token_valid,
        "market_phase":  cal["phase"],
        "is_holiday":    cal["is_holiday"],
        "entry_allowed": cal["entry_allowed"],
        "next_trading":  cal["next_trading_day"],
    }
    if request.args.get("detail") == "1":
        base["data_quality"] = _get_guardian().snapshot()
        base["calendar"]     = cal
        if _tick_recorder:
            base["tick_recorder"] = _tick_recorder.stats()
    return jsonify(base)


# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return send_from_directory(BASE_DIR, "live_dashboard.html")


# ── Startup ────────────────────────────────────────────────────────────────────

def startup():
    global _started
    if _started:
        return
    _started = True
    elog.purge_old_logs()
    elog.startup({k: v["description"] for k, v in STRATEGIES.items()})
    if not MOCK_MODE:
        t = threading.Thread(target=start_live_engine, daemon=True)
        t.start()
        elog.info("STARTUP", "Live engine thread started")
    else:
        elog.info("STARTUP", "MOCK_MODE=true — set MOCK_MODE=false for live trading")


with app.app_context():
    startup()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
