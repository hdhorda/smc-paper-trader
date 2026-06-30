"""
telegram_notifier.py — Real-time Telegram alerts for the SMC paper trading engine
==================================================================================

Setup (one-time, 5 min):
  1. Open Telegram → search @BotFather → /newbot → copy the API token
  2. Start a chat with your bot, then visit:
     https://api.telegram.org/bot<TOKEN>/getUpdates
     to get your chat_id
  3. Set env vars:
       TELEGRAM_BOT_TOKEN=<token>
       TELEGRAM_CHAT_ID=<chat_id>

Alert types sent:
  • SIGNAL_FIRED  — symbol, direction, entry/SL/TP, strategy, RR
  • POSITION_OPEN — entry taken (with slippage note)
  • POSITION_CLOSE — exit result (WIN/LOSS, P&L ₹)
  • DAILY_LOSS_CAP_HIT — circuit breaker triggered
  • POSITION_CAP_HIT   — position count cap reached
  • SIGNAL_CONFLICT    — opposing strategies warning
  • TOKEN_EXPIRED      — Kite token needs refresh
  • WS_DISCONNECT      — WebSocket dropped
  • EOD_SUMMARY        — daily wrap-up at 15:31 IST
  • ERROR              — unhandled exception (level=ERROR)

If TELEGRAM_BOT_TOKEN is not set, all calls are silent no-ops.
"""

import os
import threading
import urllib.request
import urllib.parse
import json
from datetime import datetime

_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")

# Alert filter: only send these event types (set to None to send everything)
_SEND_EVENTS = {
    "SIGNAL_FIRED", "POSITION_OPEN", "POSITION_CLOSE",
    "DAILY_LOSS_CAP_HIT", "POSITION_CAP_HIT", "SIGNAL_CONFLICT",
    "TOKEN_EXPIRED", "WS_DISCONNECT", "EOD_SUMMARY", "ERROR",
}


def _enabled() -> bool:
    return bool(_BOT_TOKEN and _CHAT_ID)


def _send(text: str):
    """Fire-and-forget Telegram message on a daemon thread."""
    if not _enabled():
        return

    def _post():
        try:
            url  = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id":    _CHAT_ID,
                "text":       text,
                "parse_mode": "HTML",
            }).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=8) as resp:
                pass   # fire and forget
        except Exception:
            pass   # never let telegram crash the engine

    threading.Thread(target=_post, daemon=True).start()


# ── Public alert functions ──────────────────────────────────────────────────────

def signal_fired(symbol: str, direction: str, strategy: str,
                 entry: float, sl: float, tp: float, tf: int):
    if "SIGNAL_FIRED" not in _SEND_EVENTS:
        return
    dir_emoji = "📈" if direction == "bull" else "📉"
    rr = round(abs(tp - entry) / abs(entry - sl), 1) if abs(entry - sl) > 0 else 0
    _send(
        f"{dir_emoji} <b>SIGNAL</b> [{strategy} {tf}min]\n"
        f"<b>{symbol}</b> {'LONG' if direction=='bull' else 'SHORT'}\n"
        f"Entry ₹{entry:.2f} | SL ₹{sl:.2f} | TP ₹{tp:.2f}\n"
        f"RR {rr}x | {datetime.now().strftime('%H:%M:%S')} IST"
    )


def position_open(symbol: str, direction: str, entry: float, sl: float, tp: float, strategy: str):
    if "POSITION_OPEN" not in _SEND_EVENTS:
        return
    dir_emoji = "🟢" if direction == "bull" else "🔴"
    _send(
        f"{dir_emoji} <b>ENTRY</b> {symbol} {'LONG' if direction=='bull' else 'SHORT'}\n"
        f"Entry ₹{entry:.2f} | SL ₹{sl:.2f} | TP ₹{tp:.2f}\n"
        f"Strategy: {strategy} | {datetime.now().strftime('%H:%M')} IST"
    )


def position_close(symbol: str, direction: str, exit_price: float,
                   reason: str, pnl_rs: float):
    if "POSITION_CLOSE" not in _SEND_EVENTS:
        return
    if pnl_rs >= 0:
        emoji = "✅"
        result = f"WIN +₹{pnl_rs:,.0f}"
    else:
        emoji = "❌"
        result = f"LOSS -₹{abs(pnl_rs):,.0f}"
    _send(
        f"{emoji} <b>EXIT</b> {symbol} [{reason}]\n"
        f"Exit ₹{exit_price:.2f} | {result}\n"
        f"{datetime.now().strftime('%H:%M')} IST"
    )


def daily_loss_cap_hit(net_pnl: float, cap: int):
    if "DAILY_LOSS_CAP_HIT" not in _SEND_EVENTS:
        return
    _send(
        f"🚨 <b>LOSS CAP HIT</b>\n"
        f"Day P&L ₹{net_pnl:+,.0f} crossed -₹{cap:,}\n"
        f"No new entries for rest of session."
    )


def position_cap_hit(symbol: str, cap: int, current_open: int):
    if "POSITION_CAP_HIT" not in _SEND_EVENTS:
        return
    _send(
        f"⚠️ <b>POSITION CAP</b>\n"
        f"Max {cap} positions reached ({current_open} open)\n"
        f"Signal for {symbol} queued."
    )


def signal_conflict(symbol: str, bull_strats: list, bear_strats: list):
    if "SIGNAL_CONFLICT" not in _SEND_EVENTS:
        return
    _send(
        f"⚡ <b>SIGNAL CONFLICT</b> {symbol}\n"
        f"LONG from: {', '.join(bull_strats)}\n"
        f"SHORT from: {', '.join(bear_strats)}\n"
        f"All signals skipped."
    )


def token_expired():
    if "TOKEN_EXPIRED" not in _SEND_EVENTS:
        return
    _send(
        "🔑 <b>TOKEN EXPIRED</b>\n"
        "Kite access token expired.\n"
        "Visit <code>/auth</code> in browser to refresh. Engine paused."
    )


def ws_disconnect(code, reason):
    if "WS_DISCONNECT" not in _SEND_EVENTS:
        return
    _send(
        f"📡 <b>WS DISCONNECTED</b>\n"
        f"Code {code}: {str(reason)[:100]}\n"
        f"Auto-reconnect in progress..."
    )


def error_alert(msg: str):
    if "ERROR" not in _SEND_EVENTS:
        return
    _send(f"🔥 <b>ERROR</b>\n{msg[:400]}")


def eod_summary(trades: int, wins: int, net_pnl: float, open_closed: int):
    if "EOD_SUMMARY" not in _SEND_EVENTS:
        return
    wr  = round(wins / trades * 100, 1) if trades else 0
    pnl_str = f"+₹{net_pnl:,.0f}" if net_pnl >= 0 else f"-₹{abs(net_pnl):,.0f}"
    _send(
        f"📊 <b>EOD SUMMARY</b> {datetime.now().strftime('%d %b %Y')}\n"
        f"Trades: {trades} | Wins: {wins} | WR: {wr}%\n"
        f"Net P&L: {pnl_str}\n"
        f"EOD exits: {open_closed} positions closed"
    )


def test_connection() -> bool:
    """Send a test message. Returns True if delivered."""
    if not _enabled():
        print("Telegram: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.", flush=True)
        return False
    try:
        url  = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": _CHAT_ID,
            "text":    "✅ SMC Paper Trading engine connected to Telegram.",
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except Exception as e:
        print(f"Telegram test failed: {e}", flush=True)
        return False
