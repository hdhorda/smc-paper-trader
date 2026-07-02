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


_S4A_SYMBOLS = [
    "ABCAPITAL","APLAPOLLO","PFC","RELIANCE","BOSCHLTD","JSWSTEEL",
    "IEX","OIL","LUPIN","GODREJCP","PAGEIND","LODHA","ALKEM",
    "DIXON","JUBLFOOD","ETERNAL","GMRAIRPORT","DABUR","BAJAJFINSV","FORCEMOT",
]
_S5_SYMBOLS = ["TRENT","AMBER","SAIL","CHOLAFIN","BANKBARODA","PGEL","BIOCON"]
_ALL_SYMBOLS = list(dict.fromkeys(_S4A_SYMBOLS + _S5_SYMBOLS))  # deduped, ordered


# ── Shadow backtest helpers ────────────────────────────────────────────────────

def _kite_client():
    """Return an authenticated KiteConnect client using env vars."""
    from kiteconnect import KiteConnect
    api_key      = os.environ.get("KITE_API_KEY", "")
    access_token = os.environ.get("KITE_ACCESS_TOKEN", "")
    if not api_key or not access_token:
        raise RuntimeError("KITE_API_KEY / KITE_ACCESS_TOKEN not set in .env")
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def _fetch_shadow_bars(kite, symbols: list, temp_dir) -> dict:
    """
    Fetch last 14 calendar days of 1-min OHLCV from Kite for each symbol.
    Saves {symbol}_1min.parquet to temp_dir.
    Returns {symbol: True/False} success map.
    """
    import pandas as pd
    from pathlib import Path
    from datetime import date

    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        instruments = kite.instruments("NSE")
        inst_map = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}
    except Exception as e:
        print(f"[shadow_bt] Could not fetch instruments: {e}")
        return {s: False for s in symbols}

    from_dt = datetime.now() - timedelta(days=14)
    to_dt   = datetime.now()
    results = {}

    for sym in symbols:
        token = inst_map.get(sym)
        if not token:
            results[sym] = False
            continue
        try:
            candles = kite.historical_data(token, from_dt, to_dt, "minute",
                                           continuous=False, oi=False)
            if not candles:
                results[sym] = False
                continue
            df = pd.DataFrame(candles).rename(columns={"date": "ts"})
            df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
            df["tradingsymbol"] = sym
            df = df[["ts","tradingsymbol","open","high","low","close","volume"]]
            df.to_parquet(temp_dir / f"{sym}_1min.parquet", index=False)
            results[sym] = True
        except Exception as e:
            print(f"[shadow_bt] {sym}: fetch error — {e}")
            results[sym] = False

    return results


def _run_shadow_backtest(temp_dir, date_str: str) -> list:
    """
    Run smc_backtest.py for date_str using temp_dir as data source.
    Returns list of trade dicts from the 'All Trades' sheet.
    """
    import subprocess, tempfile, pandas as pd
    from pathlib import Path

    temp_dir  = Path(temp_dir)
    out_dir   = temp_dir / "bt_out"
    out_dir.mkdir(exist_ok=True)
    script    = Path(__file__).parent / "smc_backtest.py"
    python    = Path(__file__).parent / ".venv" / "bin" / "python"
    if not python.exists():
        python = "python3"

    env = os.environ.copy()
    env["SMC_DATA_DIR"] = str(temp_dir)

    cmd = [
        str(python), str(script),
        "--all-tfs", "--watchlist",
        "--start", date_str, "--end", date_str,
        "--session-filter",
        "--output-dir", str(out_dir),
    ]

    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"[shadow_bt] smc_backtest error:\n{result.stderr[:500]}")
    except subprocess.TimeoutExpired:
        print("[shadow_bt] smc_backtest timed out")
        return []
    except Exception as e:
        print(f"[shadow_bt] subprocess error: {e}")
        return []

    # Find output Excel
    xl_files = list(out_dir.glob("*.xlsx"))
    if not xl_files:
        return []
    try:
        xl = pd.read_excel(xl_files[0], sheet_name=None)
        trades_df = xl.get("All Trades", pd.DataFrame())
        if trades_df.empty:
            return []
        return trades_df.to_dict("records")
    except Exception as e:
        print(f"[shadow_bt] Excel read error: {e}")
        return []


def _read_live_trades(date_str: str) -> list:
    """Read today's paper trades CSV from the logs directory."""
    import pandas as pd
    from pathlib import Path

    log_dir   = Path(os.environ.get("LOG_DIR", "/home/ubuntu/logs"))
    date_key  = date_str.replace("-", "")
    csv_path  = Path(__file__).parent / "logs" / f"paper_trades_{date_key}.csv"
    if not csv_path.exists():
        csv_path = log_dir / f"paper_trades_{date_key}.csv"
    if not csv_path.exists():
        return []
    try:
        df = pd.read_csv(csv_path)
        return df.to_dict("records") if not df.empty else []
    except Exception:
        return []


def _reconcile(live_trades: list, bt_trades: list) -> dict:
    """
    Match live trades vs backtest trades.
    Match key: symbol + direction + tf_minutes + same calendar date, entry within ±10 min.
    Returns dict with summary_lines and anomalies.
    """
    import pandas as pd

    summary   = []
    anomalies = []
    matched_bt_idx = set()

    def _ts(v):
        try:
            return pd.Timestamp(v)
        except Exception:
            return None

    # For each live trade, find a matching BT trade
    matched  = []
    missed   = []   # in BT but not live
    spurious = []   # in live but not BT

    live_matched_idx = set()

    for bi, bt in enumerate(bt_trades):
        bt_sym  = str(bt.get("symbol","")).strip()
        bt_dir  = str(bt.get("direction","")).strip()
        bt_tf   = int(bt.get("tf_minutes", 0))
        bt_ets  = _ts(bt.get("entry_ts"))

        found = False
        for li, live in enumerate(live_trades):
            if li in live_matched_idx:
                continue
            lv_sym = str(live.get("symbol","")).strip()
            lv_dir = str(live.get("direction","")).strip()
            lv_tf  = int(live.get("entry_tf", 0))
            lv_ets = _ts(live.get("entry_time"))

            if lv_sym != bt_sym or lv_dir != bt_dir or lv_tf != bt_tf:
                continue
            # Within ±10 min of each other
            if bt_ets and lv_ets and abs((bt_ets - lv_ets).total_seconds()) <= 600:
                matched.append((bt, live))
                matched_bt_idx.add(bi)
                live_matched_idx.add(li)
                found = True
                break

        if not found:
            missed.append(bt)

    for li, live in enumerate(live_trades):
        if li not in live_matched_idx:
            spurious.append(live)

    # Build summary lines
    total_bt   = len(bt_trades)
    total_live = len(live_trades)
    summary.append(f"   Backtest signals: {total_bt} | Live signals: {total_live}")

    if not bt_trades and not live_trades:
        summary.append("   ✅ Both agree — no signals today")
        return {"summary_lines": summary, "anomalies": []}

    for bt, live in matched:
        sym       = bt.get("symbol","")
        tf        = bt.get("tf_minutes","")
        direction = bt.get("direction","")
        bt_entry  = float(bt.get("entry_price", 0) or 0)
        lv_entry  = float(live.get("entry_price", 0) or 0)
        slip      = round(lv_entry - bt_entry, 2) if bt_entry else 0
        bt_pnl    = float(bt.get("pnl_pts", 0) or 0)
        lv_pnl    = float(live.get("pnl_pts", 0) or 0)
        pnl_diff  = round(lv_pnl - bt_pnl, 2)
        exit_match = bt.get("exit_reason","") == live.get("exit_reason","")
        icon      = "✅" if abs(pnl_diff) < 0.5 and exit_match else "⚠️"
        slip_str  = f"entry slip={slip:+.2f}" if abs(slip) > 0.01 else "entry match"
        pnl_str   = f"P&L slip={pnl_diff:+.2f}pts" if abs(pnl_diff) >= 0.5 else "P&L match"
        summary.append(f"   {icon} {sym} {tf}min {direction}: {slip_str} | {pnl_str} | exit={bt.get('exit_reason','?')}")
        if abs(pnl_diff) >= 1.0:
            anomalies.append(f"{sym} {tf}min: P&L divergence {pnl_diff:+.2f}pts — check fill prices")

    for bt in missed:
        sym = bt.get("symbol","?")
        tf  = bt.get("tf_minutes","?")
        d   = bt.get("direction","?")
        ets = str(bt.get("entry_ts","?"))[:16]
        summary.append(f"   ❌ MISSED: {sym} {tf}min {d} @ {ets} — in backtest, not in live")
        anomalies.append(f"MISSED {sym} {tf}min {d}: backtest found signal, live did not fire — check DATA_QUALITY / tick rate logs")

    for live in spurious:
        sym = live.get("symbol","?")
        tf  = live.get("entry_tf","?")
        d   = live.get("direction","?")
        ets = str(live.get("entry_time","?"))[:16]
        summary.append(f"   ⚠️ SPURIOUS: {sym} {tf}min {d} @ {ets} — live fired, not in backtest")
        anomalies.append(f"SPURIOUS {sym} {tf}min {d}: live fired signal not seen in backtest — verify signal_engine logic")

    if not anomalies:
        summary.append(f"   ✅ Perfect match — {len(matched)}/{total_bt} signals aligned, system working correctly")
    else:
        summary.append(f"   ⚠️ {len(anomalies)} discrepancy(ies) — review before tomorrow")

    return {"summary_lines": summary, "anomalies": anomalies}


def _check_consistency(live_signals: list, date_str: str) -> dict:
    """
    Full shadow backtest reconciliation:
    1. Fetch today's 1-min bars from Kite for all 27 symbols
    2. Run smc_backtest.py on same-day data
    3. Compare backtest signals vs live paper trades, per strategy
    """
    import tempfile, shutil

    summary   = []
    anomalies = []
    temp_dir  = None

    try:
        kite = _kite_client()
    except Exception as e:
        summary.append(f"   ⚠️ Kite client unavailable — skipping shadow backtest ({e})")
        return {"summary_lines": summary, "anomalies": []}

    try:
        temp_dir = tempfile.mkdtemp(prefix="smc_shadow_")
        print(f"[shadow_bt] Fetching bars for {len(_ALL_SYMBOLS)} symbols → {temp_dir}")
        fetch_results = _fetch_shadow_bars(kite, _ALL_SYMBOLS, temp_dir)
        ok_count = sum(1 for v in fetch_results.values() if v)
        print(f"[shadow_bt] Fetched {ok_count}/{len(_ALL_SYMBOLS)} symbols OK")

        print(f"[shadow_bt] Running backtest for {date_str}…")
        all_bt_trades  = _run_shadow_backtest(temp_dir, date_str)
        all_live_trades = _read_live_trades(date_str)
        print(f"[shadow_bt] BT trades={len(all_bt_trades)} | Live trades={len(all_live_trades)}")

        s4a_set = set(_S4A_SYMBOLS)
        s5_set  = set(_S5_SYMBOLS)

        def _filter_by_strategy(trades, sym_set, sym_key="symbol"):
            return [t for t in trades if str(t.get(sym_key,"")).strip() in sym_set]

        for strat_name, sym_set in [("S4A", s4a_set), ("S5_OB60_5", s5_set)]:
            bt_sub   = _filter_by_strategy(all_bt_trades, sym_set, "symbol")
            live_sub = _filter_by_strategy(all_live_trades, sym_set, "symbol")
            summary.append(f"\n🔬 <b>Shadow BT vs Live — {strat_name}:</b>")
            rec = _reconcile(live_sub, bt_sub)
            summary.extend(rec["summary_lines"])
            anomalies.extend(rec["anomalies"])

    except Exception as e:
        summary.append(f"   ⚠️ Shadow backtest error: {e}")
        print(f"[shadow_bt] Error: {e}")
    finally:
        if temp_dir:
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass

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
