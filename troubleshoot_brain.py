"""
troubleshoot_brain.py — SMC Paper Trading Autonomous Monitor
=============================================================
Called by three scheduled Claude agents:

  MODE=premarket   → 08:45 IST Mon-Fri  — readiness check before market opens
  MODE=monitor     → every 5 min during 09:00-15:45 IST Mon-Fri — health poll + auto-fix
  MODE=eod         → 15:45 IST Mon-Fri  — EOD summary + live vs backtest consistency report

Environment variables required (same .env as app.py):
  SERVER_URL        e.g. http://140.245.237.180:5001
  ADMIN_SECRET      matches app.py ADMIN_SECRET
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

Run:
  python troubleshoot_brain.py premarket
  python troubleshoot_brain.py monitor
  python troubleshoot_brain.py eod
"""

import os
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

IST           = ZoneInfo("Asia/Kolkata")
SERVER_URL    = os.environ.get("SERVER_URL", "http://140.245.237.180:5001").rstrip("/")
ADMIN_SECRET  = os.environ.get("ADMIN_SECRET", "")
TG_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT       = os.environ.get("TELEGRAM_CHAT_ID", "")
MODE          = sys.argv[1] if len(sys.argv) > 1 else "monitor"

# ── Telegram ───────────────────────────────────────────────────────────────────

def tg(msg: str, silent: bool = False):
    """Send a Telegram message to the user."""
    if not TG_TOKEN or not TG_CHAT:
        print(f"[TG] {msg}")
        return
    try:
        payload = json.dumps({
            "chat_id":              TG_CHAT,
            "text":                 msg,
            "parse_mode":           "HTML",
            "disable_notification": silent,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[TG ERROR] {e}")


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _get(path: str, timeout: int = 10) -> dict | None:
    try:
        with urllib.request.urlopen(f"{SERVER_URL}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _post(path: str, body: dict, token: str, timeout: int = 10) -> dict | None:
    try:
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{SERVER_URL}{path}",
            data=payload,
            headers={"Content-Type": "application/json", "X-Admin-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[POST ERROR] {path}: {e}")
        return None


def service_is_up() -> bool:
    h = _get("/api/health", timeout=8)
    return h is not None


# ── Auto-fix ───────────────────────────────────────────────────────────────────

def restart_service(reason: str) -> bool:
    """Hit /admin/restart. Returns True if accepted."""
    if not ADMIN_SECRET:
        tg("⚠️ <b>ADMIN_SECRET not set</b> — cannot auto-restart. Please check .env on server.")
        return False
    result = _post("/admin/restart", {"reason": reason}, ADMIN_SECRET)
    return result is not None and result.get("status") == "restart_initiated"


# ── PRE-MARKET CHECK (08:45 IST) ──────────────────────────────────────────────

def run_premarket():
    now = datetime.now(tz=IST).strftime("%H:%M")
    lines = [f"🌅 <b>Pre-Market Check — {now} IST</b>"]

    # 1. Is service up?
    h = _get("/api/health")
    if h is None:
        lines.append("🔴 <b>Service is DOWN</b> — attempting auto-restart...")
        ok = restart_service("pre-market check: service unreachable")
        if ok:
            time.sleep(15)
            h = _get("/api/health")
            if h:
                lines.append("✅ Service restarted successfully")
            else:
                lines.append("❌ Service still down after restart. Manual intervention needed.")
        else:
            lines.append("❌ Auto-restart failed. Please SSH to server and check.")
        tg("\n".join(lines))
        return

    lines.append("✅ Service is running")

    # 2. Token valid?
    if not h.get("token_valid"):
        lines.append(
            "🔴 <b>Kite token EXPIRED or missing</b>\n"
            f"👉 Visit <code>http://140.245.237.180:5001/auth</code> NOW to refresh before 09:15"
        )
    else:
        lines.append("✅ Kite token is valid")

    # 3. Market phase
    phase = h.get("market_phase", "?")
    lines.append(f"📅 Market phase: <b>{phase}</b>")
    if h.get("is_holiday"):
        lines.append("📴 Today is a <b>market holiday</b> — system will idle")

    lines.append("\n⏰ Market opens at 09:15 IST. Good luck today!")
    tg("\n".join(lines))


# ── MARKET HOURS MONITOR (every 5 min, 09:00–15:45 IST) ──────────────────────

# State file to track restart history and avoid alert spam
_STATE_FILE = "/tmp/tb_monitor_state.json"

def _load_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"restart_count": 0, "last_restart": None, "last_alert_issue": None, "issues_log": []}

def _save_state(s: dict):
    with open(_STATE_FILE, "w") as f:
        json.dump(s, f)

def run_monitor():
    now_ist = datetime.now(tz=IST)

    # Only run during market hours
    t = now_ist.time()
    from datetime import time as dtime
    if not (dtime(9, 0) <= t <= dtime(15, 45)):
        print(f"[TB] Outside market hours ({t}) — skipping")
        return
    if now_ist.weekday() >= 5:  # Sat/Sun
        print("[TB] Weekend — skipping")
        return

    state = _load_state()

    # ── 1. Check if service is up ──
    h = _get("/api/health")
    if h is None:
        # Service completely unreachable
        restart_count = state.get("restart_count", 0)
        if restart_count >= 3:
            tg(
                f"🚨 <b>Service DOWN — 3+ restarts attempted</b>\n"
                f"Auto-restart limit reached. Please check the server manually.\n"
                f"SSH: <code>ssh ubuntu@140.245.237.180</code>\n"
                f"Logs: <code>sudo journalctl -u smc-trader -n 50</code>"
            )
            _save_state(state)
            return

        tg(f"🔴 <b>Service unreachable at {now_ist.strftime('%H:%M')} IST</b> — attempting auto-restart #{restart_count+1}...")
        ok = restart_service("monitor: service unreachable")
        state["restart_count"] = restart_count + 1
        state["last_restart"]  = now_ist.isoformat()

        if ok:
            time.sleep(20)
            h2 = _get("/api/health")
            if h2:
                state["issues_log"].append({
                    "time": now_ist.isoformat(), "issue": "service_down",
                    "resolved": True, "action": f"auto-restart #{restart_count+1}"
                })
                tg(f"✅ Service back online after restart #{restart_count+1}")
                state["restart_count"] = 0  # reset on successful recovery
            else:
                state["issues_log"].append({
                    "time": now_ist.isoformat(), "issue": "service_down",
                    "resolved": False, "action": f"auto-restart #{restart_count+1} — still down"
                })
                tg(f"❌ Service still unreachable after restart #{restart_count+1}")
        else:
            tg("❌ /admin/restart call failed — ADMIN_SECRET may be wrong on server")

        _save_state(state)
        return

    # ── 2. Service is up — check internals ──
    issues       = h.get("issues", [])
    ws_active    = h.get("ws_active", False)
    token_valid  = h.get("token_valid", True)
    symbols_live = h.get("symbols_live", 0)
    symbols_tot  = h.get("symbols_total", 27)
    hb_age       = h.get("heartbeat_age_secs")
    low_tpm      = h.get("low_tpm_symbols", [])
    stale        = h.get("stale_symbols", [])
    phase        = h.get("market_phase", "?")

    # Reset restart counter when service is healthy
    if not issues:
        state["restart_count"] = 0

    # ── 3. Token expired ──
    if not token_valid:
        if state.get("last_alert_issue") != "token_expired":
            tg(
                f"🔴 <b>Token Expired — {now_ist.strftime('%H:%M')} IST</b>\n"
                f"Signals are blocked. Visit <code>http://140.245.237.180:5001/auth</code> now."
            )
            state["last_alert_issue"]  = "token_expired"
            state["issues_log"].append({"time": now_ist.isoformat(), "issue": "token_expired", "resolved": False, "action": "user alerted"})
        _save_state(state)
        return

    # ── 4. WebSocket down but service is up → restart ──
    if not ws_active and phase in ("TRADING", "EOD_EXIT", "WARMUP"):
        tg(f"🔴 <b>WebSocket disconnected — {now_ist.strftime('%H:%M')} IST</b>\nAuto-restarting service...")
        ok = restart_service("monitor: websocket down during market hours")
        state["restart_count"] = state.get("restart_count", 0) + 1
        resolved = False
        if ok:
            time.sleep(25)
            h2 = _get("/api/health")
            if h2 and h2.get("ws_active"):
                tg("✅ WebSocket reconnected after service restart")
                resolved = True
                state["restart_count"] = 0
            else:
                tg("⚠️ Service restarted but WebSocket still not active. Will retry next check.")
        state["issues_log"].append({"time": now_ist.isoformat(), "issue": "ws_down", "resolved": resolved, "action": "auto-restart"})
        _save_state(state)
        return

    # ── 5. Widespread DATA_QUALITY degradation ──
    if len(low_tpm) >= 10:  # > 1/3 of symbols degraded
        # Only restart if heartbeat is also stale (real problem, not just a quiet market)
        if hb_age and hb_age > 400:
            tg(
                f"🟡 <b>DATA_QUALITY degraded — {now_ist.strftime('%H:%M')} IST</b>\n"
                f"{len(low_tpm)} symbols below 10 TPM, heartbeat {hb_age}s ago\n"
                f"Auto-restarting..."
            )
            ok = restart_service("monitor: widespread data quality degradation")
            state["issues_log"].append({"time": now_ist.isoformat(), "issue": "low_tpm_widespread", "resolved": ok, "action": "auto-restart"})
            _save_state(state)
            return
        else:
            # Quiet market period — warn only, don't restart
            if state.get("last_alert_issue") != "low_tpm_quiet":
                tg(
                    f"🟡 <b>Low tick rate — {now_ist.strftime('%H:%M')} IST</b>\n"
                    f"{len(low_tpm)} symbols below 10 TPM — may be market quiet period\n"
                    f"Symbols: {', '.join(low_tpm[:8])}{'...' if len(low_tpm)>8 else ''}",
                    silent=True
                )
                state["last_alert_issue"] = "low_tpm_quiet"

    # ── 6. Heartbeat stale (no activity for > 10 min) ──
    # Only restart during active market phases — post-market heartbeat silence is normal
    elif hb_age and hb_age > 700 and phase in ("TRADING", "EOD_EXIT", "WARMUP"):
        tg(
            f"🟡 <b>Heartbeat stale — {now_ist.strftime('%H:%M')} IST</b>\n"
            f"Last heartbeat {hb_age//60}m ago. Service may be frozen.\n"
            f"Restarting..."
        )
        ok = restart_service("monitor: heartbeat stale >10min")
        state["issues_log"].append({"time": now_ist.isoformat(), "issue": "heartbeat_stale", "resolved": ok, "action": "auto-restart"})
        _save_state(state)
        return

    else:
        # All clear — silent, no message (don't spam Telegram every 5 min)
        state["last_alert_issue"] = None
        state["restart_count"]    = 0

    _save_state(state)
    print(f"[TB] {now_ist.strftime('%H:%M')} — OK | ws={ws_active} live={symbols_live}/{symbols_tot} hb={hb_age}s")


# ── EOD SUMMARY + CONSISTENCY CHECK (15:45 IST) ───────────────────────────────

def run_eod():
    now_ist  = datetime.now(tz=IST)
    date_str = now_ist.strftime("%Y-%m-%d")
    state    = _load_state()

    # ── Fetch today's data ──
    today = _get("/api/today")
    health = _get("/api/health")

    signals      = today.get("signals", [])         if today else []
    closed       = today.get("closed_trades", [])   if today else []
    open_pos     = today.get("open_positions", [])   if today else []
    net_pnl      = today.get("net_pnl", 0.0)        if today else 0.0
    trades_count = today.get("trades_count", 0)      if today else 0
    wins         = today.get("wins", 0)              if today else 0
    win_rate     = today.get("win_rate", 0.0)        if today else 0.0

    issues_log   = state.get("issues_log", [])
    resolved     = [i for i in issues_log if i.get("resolved")]
    unresolved   = [i for i in issues_log if not i.get("resolved")]

    # ── Build EOD report ──
    lines = [
        f"📊 <b>SMC Trading EOD Summary — {date_str}</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]

    # System health during session
    restart_count = state.get("restart_count", 0)
    total_issues  = len(issues_log)
    if total_issues == 0:
        lines.append("✅ <b>System:</b> No issues today — ran clean all session")
    else:
        lines.append(f"⚙️ <b>System issues today: {total_issues}</b>")
        for iss in issues_log:
            t    = iss.get("time", "")[:16].replace("T"," ")
            icon = "✅" if iss.get("resolved") else "❌"
            lines.append(f"  {icon} {t} — {iss.get('issue')} → {iss.get('action')}")

    lines.append("")

    # Signals summary
    s4a_sigs = [s for s in signals if s.get("strategy") == "S4A"]
    s5_sigs  = [s for s in signals if s.get("strategy") == "S5_OB60_5"]
    lines.append(f"📡 <b>Signals fired:</b> {len(signals)} total")
    lines.append(f"   S4A: {len(s4a_sigs)} | S5_OB60_5: {len(s5_sigs)}")

    if signals:
        bull = sum(1 for s in signals if s.get("direction") == "bull")
        bear = sum(1 for s in signals if s.get("direction") == "bear")
        lines.append(f"   Bull: {bull} | Bear: {bear}")
        # List symbols
        syms = list(dict.fromkeys(s.get("symbol","") for s in signals))
        lines.append(f"   Symbols: {', '.join(syms[:10])}{'...' if len(syms)>10 else ''}")

    lines.append("")

    # P&L summary
    lines.append(f"💰 <b>Paper P&L:</b>")
    lines.append(f"   Trades closed: {trades_count} | Wins: {wins} | Win rate: {win_rate:.0f}%")
    pnl_icon = "🟢" if net_pnl >= 0 else "🔴"
    lines.append(f"   Net P&L: {pnl_icon} <b>₹{net_pnl:+,.0f}</b>")
    if open_pos:
        lines.append(f"   Open at close: {len(open_pos)} positions (will carry to tomorrow)")

    lines.append("")

    # ── Consistency check ──
    lines.append("🔬 <b>Live vs Backtest Consistency:</b>")
    consistency_result = _check_consistency(signals, date_str)
    lines.extend(consistency_result["summary_lines"])

    lines.append("")

    # Pending items
    if unresolved:
        lines.append(f"⚠️ <b>Pending issues ({len(unresolved)}) — resolve before tomorrow:</b>")
        for u in unresolved:
            lines.append(f"   ❌ {u.get('issue')} — {u.get('action')}")
    else:
        lines.append("✅ <b>No pending issues</b> — ready for tomorrow")

    lines.append("")
    lines.append("⏰ Pre-market check tomorrow at 08:45 IST")
    lines.append("   Remember to refresh Kite token before 09:15!")

    tg("\n".join(lines))

    # Reset daily state
    state["issues_log"]       = []
    state["restart_count"]    = 0
    state["last_alert_issue"] = None
    _save_state(state)
    print("[TB] EOD summary sent and state reset")


def _check_consistency(live_signals: list, date_str: str) -> dict:
    """
    Compare today's live signals against backtest expectations.

    Checks:
    1. Do live signals come from the correct strategy watchlists?
    2. Are directions (bull/bear) consistent with symbols' backtest bias?
    3. Are risk:reward ratios in expected range?
    4. Flag any symbols that fired live but were NOT in the backtested watchlist.
    """
    # Backtest-validated watchlists (from S4A and S5_OB60_5 deployment config)
    S4A_SYMBOLS = {
        "ABCAPITAL","APLAPOLLO","PFC","RELIANCE","BOSCHLTD","JSWSTEEL",
        "IEX","OIL","LUPIN","GODREJCP","PAGEIND","LODHA","ALKEM",
        "DIXON","JUBLFOOD","ETERNAL","GMRAIRPORT","DABUR","BAJAJFINSV","FORCEMOT",
    }
    S5_SYMBOLS = {"TRENT","AMBER","SAIL","CHOLAFIN","BANKBARODA","PGEL","BIOCON"}

    summary = []
    anomalies = []

    if not live_signals:
        summary.append("   No signals fired today — nothing to compare")
        return {"summary_lines": summary, "anomalies": []}

    for sig in live_signals:
        sym      = sig.get("symbol", "")
        strategy = sig.get("strategy", "")
        direction= sig.get("direction", "")
        entry    = sig.get("entry_price", 0)
        sl       = sig.get("sl", 0)
        tp       = sig.get("tp", 0)

        # Check 1: Symbol is in correct watchlist?
        expected_syms = S4A_SYMBOLS if strategy == "S4A" else S5_SYMBOLS
        if sym not in expected_syms:
            anomalies.append(f"❌ {sym} fired on {strategy} but NOT in that strategy's watchlist")
            continue

        # Check 2: R:R ratio
        if entry and sl and tp:
            risk    = abs(entry - sl)
            reward  = abs(tp - entry)
            rr      = round(reward / risk, 2) if risk > 0 else 0
            if rr < 1.5:
                anomalies.append(f"⚠️ {sym} {strategy}: R:R={rr} below 1.5 — check SL/TP logic")
            elif rr > 10:
                anomalies.append(f"⚠️ {sym} {strategy}: R:R={rr} unusually high — possible data issue")

    # Check 3: Strategy signal counts vs historical rate
    # S4A historically fires 2-8 signals/day on 20 symbols; >15 is suspicious
    s4a_count = sum(1 for s in live_signals if s.get("strategy") == "S4A")
    s5_count  = sum(1 for s in live_signals if s.get("strategy") == "S5_OB60_5")

    if s4a_count > 15:
        anomalies.append(f"⚠️ S4A fired {s4a_count} signals today — unusually high (backtest avg: 3-6/day). Check for over-triggering.")
    if s5_count > 5:
        anomalies.append(f"⚠️ S5_OB60_5 fired {s5_count} signals — higher than expected. Verify HTF OB logic.")

    if anomalies:
        summary.append(f"   ⚠️ {len(anomalies)} inconsistency(ies) found:")
        summary.extend(f"      {a}" for a in anomalies)
        summary.append("   → Review signal_engine.py logic vs backtest parameters")
    else:
        summary.append(f"   ✅ All {len(live_signals)} signal(s) consistent with backtest expectations")
        if live_signals:
            summary.append(f"   Watchlist coverage: OK | R:R ratios: OK | Signal count: OK")

    return {"summary_lines": summary, "anomalies": anomalies}


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if MODE == "premarket":
        run_premarket()
    elif MODE == "monitor":
        run_monitor()
    elif MODE == "eod":
        run_eod()
    else:
        print(f"Unknown mode: {MODE}. Use: premarket | monitor | eod")
        sys.exit(1)
