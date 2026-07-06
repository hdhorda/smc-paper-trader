"""
Gap-Down Bounce — premarket scanner & paper trader (standalone)
===============================================================
Strategy (rule FROZEN 2026-07-04 — do not tune without a new validation):
  - At open: count universe stocks with -8% <= gap <= -2% vs previous close
  - Trade ONLY if 8-30 stocks qualify (market-stress day)
  - LONG the deepest <=10 gappers at the close of the first 5min bar (09:20)
  - Exit at 15:15. No stop. Rs 2,00,000 per position (paper).
Validated: net PF 1.80, +0.62%/trade, 7/7 years positive 2020-2026.
See vault: 'Backtest - Gap-Down Bounce (Stress Days)'.

Completely independent of the smc-trader service. No WebSocket.

Modes:
  --scan    (cron 09:15 IST):  waits to 09:15:30, reads quotes, selects
            entries, waits to 09:20:02, records paper entries, Telegram.
  --eod     (cron 15:16 IST):  closes open paper positions at LTP, logs
            results, Telegram summary.
  --replay YYYY-MM-DD : offline validation against local 1-min parquets
            (needs SMC_DATA_DIR or Historicalcash one level up). Prints what
            the scanner would have done — must match gapdown.py backtest.

Cron (ubuntu user, IST):
  14 9 * * 1-5   cd /home/smcbot/smc-trader && python3 gap_scanner.py --scan >> /home/ubuntu/logs/gap_scanner.log 2>&1
  16 15 * * 1-5  cd /home/smcbot/smc-trader && python3 gap_scanner.py --eod  >> /home/ubuntu/logs/gap_scanner.log 2>&1
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = Path(os.environ.get("GAP_LOG_DIR", str(BASE_DIR / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Shared SQLite db (same paper_trades.db used by app.py) ────────────────────
# Use absolute path so cron working-directory doesn't matter.
os.environ.setdefault("DB_PATH", str(BASE_DIR / "paper_trades.db"))
sys.path.insert(0, str(BASE_DIR))
import db as _db
_db.init_db()   # no-op if tables already exist


def _db_insert_position(pos: dict) -> int:
    """Insert an open GapDown position into the shared trades table.
    Returns the db row id (stored in positions JSON so EOD can close it)."""
    _db.insert_signal({
        "fired_at":    pos["entry_time"],
        "symbol":      pos["symbol"],
        "strategy":    "GapDown",
        "direction":   "bull",
        "entry_price": pos["entry_price"],
        "sl_price":    None,
        "tp_price":    None,
        "entry_tf":    None,
        "htf_signal":  "gap_down",
        "fvg_top":     None,
        "fvg_bottom":  None,
        "pd_zone":     "discount",
    })
    return _db.insert_trade({
        "symbol":      pos["symbol"],
        "strategy":    "GapDown",
        "direction":   "bull",
        "entry_time":  pos["entry_time"],
        "entry_price": pos["entry_price"],
        "sl_price":    None,
        "tp_price":    None,
        "entry_tf":    None,
        "pd_zone":     "discount",
        "htf_signal":  "gap_down",
    })


def _db_close_position(trade_id: int, exit_price: float,
                        entry_price: float, qty: int, exit_time: str):
    """Mark a GapDown trade as closed in the shared db."""
    pnl_pts = round(exit_price - entry_price, 4)
    pnl_pct = round(pnl_pts / entry_price * 100, 4) if entry_price else 0
    pnl_rs  = round(qty * pnl_pts, 2)
    _db.close_trade(trade_id, {
        "exit_time":   exit_time,
        "exit_price":  exit_price,
        "exit_reason": "EOD_EXIT",
        "pnl_pts":     pnl_pts,
        "pnl_pct":     pnl_pct,
        "pnl_rs":      pnl_rs,
        "win":         1 if pnl_rs > 0 else 0,
    })

# ── Frozen parameters ─────────────────────────────────────────────────────────
GAP_MIN, GAP_MAX = -8.0, -2.0
MIN_SIGNALS, MAX_SIGNALS = 8, 30
MAX_POSITIONS = 10
POSITION_RS = 200_000

UNIVERSE = [s.strip() for s in open(BASE_DIR / "gap_universe.txt").read().splitlines() if s.strip()]


def load_env():
    envp = BASE_DIR / ".env"
    if envp.exists():
        for line in envp.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def telegram(msg: str):
    try:
        import requests
        tok = os.environ.get("TG_TOKEN")
        chat = os.environ.get("TG_CHAT")
        if not tok or not chat:
            print("[tg] not configured:", msg)
            return
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": msg}, timeout=10)
    except Exception as e:
        print("[tg] failed:", e)


def kite_client():
    from kiteconnect import KiteConnect
    kc = KiteConnect(api_key=os.environ["KITE_API_KEY"])
    kc.set_access_token(os.environ["KITE_ACCESS_TOKEN"])
    return kc


def get_quotes(kc, symbols):
    """Batch quote fetch (Kite allows up to 500). Returns {sym: quote}."""
    keys = [f"NSE:{s}" for s in symbols]
    out = {}
    for i in range(0, len(keys), 400):
        try:
            q = kc.quote(keys[i:i + 400])
            for k, v in q.items():
                out[k.split(":", 1)[1]] = v
        except Exception as e:
            print(f"[quote] batch {i} failed: {e}")
    return out


def wait_until(hh, mm, ss):
    while True:
        now = datetime.now()
        if (now.hour, now.minute, now.second) >= (hh, mm, ss):
            return
        time.sleep(0.5)


def positions_path(d: str) -> Path:
    return LOG_DIR / f"gap_positions_{d}.json"


def trades_csv() -> Path:
    p = LOG_DIR / "gap_trades.csv"
    if not p.exists():
        with open(p, "w", newline="") as f:
            csv.writer(f).writerow([
                "date", "symbol", "gap_pct", "n_signals", "qty",
                "entry_time", "entry_price", "exit_time", "exit_price",
                "pnl_pct", "pnl_rs", "win"])
    return p


# ── live scan (09:14 cron) ────────────────────────────────────────────────────

def mode_scan():
    load_env()
    today = date.today().isoformat()
    try:
        sys.path.insert(0, str(BASE_DIR))
        import market_calendar as mkt
        if hasattr(mkt, "is_trading_day") and not mkt.is_trading_day():
            print(f"{today}: holiday — skip")
            return
    except Exception:
        pass  # calendar unavailable: proceed (cron already limits Mon-Fri)

    kc = kite_client()
    wait_until(9, 15, 30)
    quotes = get_quotes(kc, UNIVERSE)

    cands = []
    for s, q in quotes.items():
        try:
            o = q["ohlc"]["open"]
            pc = q["ohlc"]["close"]          # previous session close during live day
            if not o or not pc:
                continue
            gap = (o - pc) / pc * 100
            if GAP_MIN <= gap <= GAP_MAX:
                cands.append((s, gap))
        except Exception:
            continue

    n = len(cands)
    print(f"{today} 09:15:30 — {n} qualifying gap-downs")
    if not (MIN_SIGNALS <= n <= MAX_SIGNALS):
        telegram(f"GapDown {today}: {n} signals — outside 8-30 band, NO TRADE day.")
        return

    picks = sorted(cands, key=lambda x: x[1])[:MAX_POSITIONS]
    telegram(f"GapDown {today}: STRESS DAY ({n} signals). Entering at 09:20: " +
             ", ".join(f"{s} {g:.1f}%" for s, g in picks))

    wait_until(9, 20, 2)
    entry_quotes = get_quotes(kc, [s for s, _ in picks])
    positions = []
    for s, gap in picks:
        q = entry_quotes.get(s)
        if not q:
            continue
        px = float(q["last_price"])
        if px <= 0:
            continue
        qty = int(POSITION_RS / px)
        entry_time = datetime.now().isoformat(timespec="seconds")
        pos = {"symbol": s, "gap_pct": round(gap, 3), "n_signals": n,
               "qty": qty, "entry_time": entry_time, "entry_price": px}
        try:
            pos["db_trade_id"] = _db_insert_position(pos)
        except Exception as e:
            print(f"  [db] insert failed for {s}: {e}")
            pos["db_trade_id"] = None
        positions.append(pos)
        print(f"  PAPER LONG {s} x{qty} @ {px} (gap {gap:.2f}%)")
    json.dump(positions, open(positions_path(today), "w"), indent=1)
    telegram(f"GapDown {today}: {len(positions)} paper entries placed. EOD exit 15:15.")


# ── EOD close (15:16 cron) ────────────────────────────────────────────────────

def mode_eod():
    load_env()
    today = date.today().isoformat()
    pp = positions_path(today)
    if not pp.exists():
        print(f"{today}: no positions file — nothing to close")
        return
    positions = json.load(open(pp))
    if not positions:
        return
    kc = kite_client()
    quotes = get_quotes(kc, [p["symbol"] for p in positions])
    rows, total = [], 0.0
    exit_time = datetime.now().isoformat(timespec="seconds")
    for p in positions:
        q = quotes.get(p["symbol"])
        xp = float(q["last_price"]) if q else p["entry_price"]
        pnl_pct = (xp - p["entry_price"]) / p["entry_price"] * 100
        pnl_rs = p["qty"] * (xp - p["entry_price"])
        total += pnl_rs
        rows.append([today, p["symbol"], p["gap_pct"], p["n_signals"], p["qty"],
                     p["entry_time"], p["entry_price"],
                     exit_time, xp,
                     round(pnl_pct, 4), round(pnl_rs, 2), pnl_pct > 0])
        # Write close to shared db
        trade_id = p.get("db_trade_id")
        if trade_id:
            try:
                _db_close_position(trade_id, xp, p["entry_price"], p["qty"], exit_time)
            except Exception as e:
                print(f"  [db] close failed for {p['symbol']}: {e}")
    with open(trades_csv(), "a", newline="") as f:
        csv.writer(f).writerows(rows)
    wins = sum(1 for r in rows if r[-1])
    telegram(f"GapDown {today} EOD: {len(rows)} closed | {wins}W/{len(rows)-wins}L | "
             f"net {'+' if total >= 0 else ''}{total:,.0f} Rs (gross, ex-costs)")
    print(f"{today}: closed {len(rows)}, net {total:,.0f}")


# ── offline replay validation ─────────────────────────────────────────────────

def mode_replay(day: str):
    """Reproduce the scanner's decisions for a past date from local parquets."""
    import pandas as pd
    data_dir = Path(os.environ.get("SMC_DATA_DIR", str(BASE_DIR.parent / "Historicalcash")))
    target = pd.Timestamp(day).date()
    cands = []
    for s in UNIVERSE:
        f = data_dir / f"{s}_1min.parquet"
        if not f.exists():
            continue
        lo = pd.Timestamp(target) - pd.Timedelta(days=7)
        hi = pd.Timestamp(target) + pd.Timedelta(days=1)
        try:
            df = pd.read_parquet(f, columns=["ts", "open", "close"],
                                 filters=[("ts", ">=", lo.tz_localize("Asia/Kolkata")),
                                          ("ts", "<", hi.tz_localize("Asia/Kolkata"))])
        except Exception:
            df = pd.read_parquet(f, columns=["ts", "open", "close"])
        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
        df["d"] = df["ts"].dt.date
        day_rows = df[df["d"] == target]
        prev_rows = df[df["d"] < target]
        if day_rows.empty or prev_rows.empty:
            continue
        o = float(day_rows.iloc[0]["open"])
        pc = float(prev_rows.iloc[-1]["close"])
        gap = (o - pc) / pc * 100
        if GAP_MIN <= gap <= GAP_MAX:
            # entry = close of first 5min bar = close of the 09:19 1-min bar
            first5 = day_rows[day_rows["ts"].dt.time <= pd.Timestamp("09:19").time()]
            entry = float(first5.iloc[-1]["close"])
            eod = float(day_rows.iloc[-1]["close"])
            cands.append((s, gap, entry, eod))
    n = len(cands)
    print(f"REPLAY {day}: {n} qualifying gap-downs -> " +
          ("STRESS DAY (would trade)" if MIN_SIGNALS <= n <= MAX_SIGNALS else "no-trade day"))
    if MIN_SIGNALS <= n <= MAX_SIGNALS:
        picks = sorted(cands, key=lambda x: x[1])[:MAX_POSITIONS]
        tot = 0.0
        for s, gap, entry, eod in picks:
            pnl = (eod - entry) / entry * 100
            tot += pnl
            print(f"  {s:12s} gap {gap:6.2f}% | entry 09:20 {entry:10.2f} | exit 15:15 {eod:10.2f} | {pnl:+.2f}%")
        print(f"  basket avg: {tot/len(picks):+.3f}%")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--eod", action="store_true")
    ap.add_argument("--replay", type=str, default=None)
    a = ap.parse_args()
    if a.replay:
        mode_replay(a.replay)
    elif a.scan:
        mode_scan()
    elif a.eod:
        mode_eod()
    else:
        ap.print_help()
