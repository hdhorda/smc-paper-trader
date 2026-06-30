"""
event_logger.py — Structured JSON event log book
=================================================
Every meaningful system event is written as a JSONL entry to logs/events_YYYY-MM-DD.jsonl

Event types:
  STARTUP          App started, strategy config loaded
  WARMUP_START     Beginning historical data fetch
  WARMUP_DONE      Historical data loaded successfully
  WARMUP_FAIL      Symbol failed warmup (with retry count)
  WS_CONNECT       Kite WebSocket connected
  WS_DISCONNECT    WebSocket dropped
  WS_RECONNECT     Reconnect attempt (with attempt number)
  WS_ERROR         WebSocket error
  TOKEN_EXPIRED    Kite access token expired (403 received)
  TOKEN_REFRESHED  New access token set
  BAR_CLOSED       1min bar completed for a symbol
  SIGNAL_FIRED     Strategy signal generated
  POSITION_OPEN    Paper position opened
  POSITION_CLOSE   Paper position closed (with P&L)
  MARKET_OPEN      Market session started
  MARKET_CLOSE     Market session ended / EOD cleanup
  HEARTBEAT        Periodic alive-check during market hours
  ERROR            Unexpected exception (with traceback summary)

Each entry: {"ts":..., "level":..., "event":..., "msg":..., "data":{...}}
Levels: INFO, WARN, ERROR
"""

import json
import os
import traceback
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(os.environ.get("LOG_DIR", "logs"))
KEEP_DAYS = int(os.environ.get("LOG_KEEP_DAYS", "30"))   # auto-purge logs older than this

# In-memory ring buffer for fast /api/logs serving (last 500 events)
_ring: list[dict] = []
_RING_SIZE = 500


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _write(entry: dict):
    """Append entry to today's JSONL file and in-memory ring."""
    global _ring
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"events_{_today()}.jsonl"
    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # never let logging crash the engine

    _ring.append(entry)
    if len(_ring) > _RING_SIZE:
        _ring = _ring[-_RING_SIZE:]

    # Print to stdout (visible in Render/Railway log viewer)
    lvl = entry.get("level", "INFO")
    prefix = {"INFO": "  ", "WARN": "⚠ ", "ERROR": "✖ "}.get(lvl, "  ")
    print(f"[{entry['ts']}] {prefix}[{entry['event']}] {entry['msg']}", flush=True)


# ── Public API ──────────────────────────────────────────────────────────────────

def info(event: str, msg: str, data: dict = None):
    _write({"ts": datetime.now().isoformat(), "level": "INFO",
            "event": event, "msg": msg, **({"data": data} if data else {})})


def warn(event: str, msg: str, data: dict = None):
    _write({"ts": datetime.now().isoformat(), "level": "WARN",
            "event": event, "msg": msg, **({"data": data} if data else {})})


def error(event: str, msg: str, exc: Exception = None, data: dict = None):
    entry = {"ts": datetime.now().isoformat(), "level": "ERROR",
             "event": event, "msg": msg}
    extra = {}
    if exc:
        extra["exception"] = type(exc).__name__
        extra["traceback"] = traceback.format_exc(limit=5)
    if data:
        extra.update(data)
    if extra:
        entry["data"] = extra
    _write(entry)
    # Forward ERROR events to Telegram (lazy import avoids circular dependency)
    try:
        import telegram_notifier as _tg
        _tg.error_alert(f"[{event}] {msg}")
    except Exception:
        pass


# ── Convenience wrappers ────────────────────────────────────────────────────────

def startup(config_summary: dict):
    info("STARTUP", f"System starting — strategies: {list(config_summary.keys())}", config_summary)


def warmup_start(symbols: list):
    info("WARMUP_START", f"Fetching historical data for {len(symbols)} symbols", {"symbols": symbols})


def warmup_done(results: dict):
    ok  = sum(1 for v in results.values() if v)
    bad = [s for s, v in results.items() if not v]
    msg = f"Warmup complete: {ok}/{len(results)} symbols OK"
    if bad:
        warn("WARMUP_DONE", msg + f" | failed: {bad}", {"failed": bad})
    else:
        info("WARMUP_DONE", msg)


def warmup_fail(symbol: str, attempt: int, reason: str):
    warn("WARMUP_FAIL", f"{symbol} warmup attempt {attempt} failed: {reason}",
         {"symbol": symbol, "attempt": attempt})


def ws_connect(n_instruments: int):
    info("WS_CONNECT", f"WebSocket connected: {n_instruments} instruments subscribed")


def ws_disconnect(code, reason):
    warn("WS_DISCONNECT", f"WebSocket dropped — code={code} reason={reason}",
         {"code": code, "reason": str(reason)})


def ws_reconnect(attempt: int, delay_s: int):
    warn("WS_RECONNECT", f"Reconnect attempt {attempt} in {delay_s}s")


def ws_error(code, reason):
    error("WS_ERROR", f"WebSocket error — code={code}", data={"code": code, "reason": str(reason)})


def token_expired():
    error("TOKEN_EXPIRED",
          "Kite access token expired. Visit /auth in browser to refresh (takes 30 sec).")


def token_refreshed():
    info("TOKEN_REFRESHED", "New Kite access token set successfully")


def signal_fired(symbol: str, strategy: str, direction: str, tf: int, price: float):
    info("SIGNAL_FIRED", f"{symbol} {direction} @ {price:.2f} [{strategy} {tf}min]",
         {"symbol": symbol, "strategy": strategy, "direction": direction,
          "tf": tf, "price": price})


def position_open(symbol: str, direction: str, entry: float, sl: float, tp: float):
    rr = round(abs(tp - entry) / abs(entry - sl), 1) if abs(entry - sl) > 0 else 0
    info("POSITION_OPEN", f"{symbol} {direction} entry={entry:.2f} SL={sl:.2f} TP={tp:.2f} RR={rr}x",
         {"symbol": symbol, "direction": direction,
          "entry": entry, "sl": sl, "tp": tp, "rr": rr})


def position_close(symbol: str, direction: str, entry: float, exit_p: float,
                   reason: str, pnl_rs: float):
    won = "WIN" if pnl_rs > 0 else "LOSS"
    info("POSITION_CLOSE",
         f"{symbol} {direction} EXIT={exit_p:.2f} [{reason}] {won} ₹{pnl_rs:+.0f}",
         {"symbol": symbol, "direction": direction,
          "entry": entry, "exit": exit_p, "reason": reason, "pnl_rs": pnl_rs})


def heartbeat(open_positions: int, closed_today: int, net_pnl_today: float):
    info("HEARTBEAT",
         f"Alive | open={open_positions} closed_today={closed_today} net_pnl=₹{net_pnl_today:+.0f}",
         {"open": open_positions, "closed_today": closed_today, "net_pnl": net_pnl_today})


def market_open():
    info("MARKET_OPEN", "Market session started — scanning active")


def market_close(closed_positions: int, net_pnl: float):
    info("MARKET_CLOSE",
         f"Market closed — EOD exits: {closed_positions} | day P&L ₹{net_pnl:+.0f}",
         {"eod_exits": closed_positions, "net_pnl": net_pnl})


# ── Query ───────────────────────────────────────────────────────────────────────

def get_recent(limit: int = 100, level: str = None, event_type: str = None) -> list[dict]:
    """Return recent events from in-memory ring, newest first."""
    events = list(reversed(_ring))
    if level:
        events = [e for e in events if e.get("level") == level.upper()]
    if event_type:
        events = [e for e in events if e.get("event") == event_type.upper()]
    return events[:limit]


def get_from_file(date_str: str = None, limit: int = 200) -> list[dict]:
    """Read events from JSONL file for a specific date."""
    if not date_str:
        date_str = _today()
    log_file = LOG_DIR / f"events_{date_str}.jsonl"
    if not log_file.exists():
        return []
    events = []
    with open(log_file) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    return list(reversed(events))[-limit:]


def purge_old_logs():
    """Delete log files older than KEEP_DAYS. Call on startup."""
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    for f in LOG_DIR.glob("events_*.jsonl"):
        try:
            file_date = datetime.strptime(f.stem.replace("events_", ""), "%Y-%m-%d")
            if file_date < cutoff:
                f.unlink()
                print(f"Purged old log: {f.name}", flush=True)
        except Exception:
            pass
