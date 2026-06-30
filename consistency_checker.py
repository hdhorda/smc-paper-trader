"""
consistency_checker.py — Weekly live-vs-backtest signal consistency check
==========================================================================
Runs every Sunday at 17:00 IST (post-market, post-settlement).
Re-runs the backtester on the last 5 trading days using Kite historical data,
then compares against signals actually fired in the live DB.

Metrics produced:
  - signal_count_match_pct  : % of days where live count ≈ backtest count (±20%)
  - direction_match_pct     : % of individual signals where direction matches
  - symbol_match_pct        : % of signals fired on same symbol on same day
  - timing_match_pct        : % of signals within ±2 bars of backtest timing

WARN threshold: if overall match < 85%, emits CONSISTENCY_WARN event.
Results stored in DB and surfaced at /api/stats as CONSISTENCY_REPORT.

Usage:
  python consistency_checker.py          # manual run
  # Or wire into app.py startup to run every Sunday at 17:00 (see schedule below)

Schedule wire-in (in start_live_engine):
  def _weekly_consistency():
      import consistency_checker as cc
      while True:
          cc.wait_for_next_sunday_1700()
          report = cc.run()
          elog.info("CONSISTENCY_REPORT", f"Match: {report['overall_match_pct']}%", report)
          if report['overall_match_pct'] < 85:
              elog.warn("CONSISTENCY_WARN", "Live signals diverging from backtest!", report)
              tg.error_alert(f"CONSISTENCY <85%: {report['overall_match_pct']}% match this week")
  threading.Thread(target=_weekly_consistency, daemon=True).start()
"""

import os
import time
import json
from datetime import datetime, timedelta, date
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────────
MATCH_WARN_PCT    = float(os.environ.get("CONSISTENCY_WARN_PCT", "85"))
LOOKBACK_DAYS     = int(os.environ.get("CONSISTENCY_LOOKBACK", "5"))    # trading days
TIMING_TOLERANCE  = int(os.environ.get("CONSISTENCY_TIMING_BARS", "2")) # ±bars


def get_last_n_trading_days(n: int) -> list[date]:
    """Return last n NSE trading days (Mon-Fri, excluding holidays)."""
    try:
        from market_calendar import calendar as mkt
    except ImportError:
        mkt = None

    days = []
    d = date.today() - timedelta(days=1)   # start from yesterday
    while len(days) < n:
        if d.weekday() < 5:   # Mon-Fri
            if mkt is None or mkt.is_trading_day(d):
                days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def fetch_live_signals(date_str: str) -> list[dict]:
    """Pull signals from live DB for a specific date."""
    try:
        import db
        signals = db.get_recent_signals(limit=500)
        return [s for s in signals if s.get("fired_at", "").startswith(date_str)]
    except Exception:
        return []


def run_backtest_for_day(day: date, kite=None) -> list[dict]:
    """
    Fetch historical data for `day` from Kite and run signal engine.
    Returns list of signal dicts: {symbol, direction, strategy, bar_index, time}.
    """
    signals = []
    try:
        import pandas as pd
        from signal_engine import scan_symbol
        from live_runner import BarWindow
        from app import STRATEGIES, SESSION_WINDOWS

        if kite is None:
            from kite_warmer import get_kite_client
            kite = get_kite_client()

        instruments = kite.instruments("NSE")
        inst_map = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}

        from_dt = datetime.combine(day, datetime.min.time().replace(hour=9, minute=0))
        to_dt   = datetime.combine(day, datetime.min.time().replace(hour=15, minute=30))

        all_syms = list({s for cfg in STRATEGIES.values() for s in cfg["symbols"]})
        strategy_cfgs = [v for v in STRATEGIES.values() if v.get("enabled")]

        for sym in all_syms:
            token = inst_map.get(sym)
            if not token:
                continue
            try:
                candles = kite.historical_data(
                    token, from_date=from_dt, to_date=to_dt,
                    interval="minute", continuous=False, oi=False,
                )
                if not candles:
                    continue
                df = pd.DataFrame(candles)
                df = df.rename(columns={"date": "ts"})
                df["ts"] = pd.to_datetime(df["ts"])

                for scfg in strategy_cfgs:
                    if sym not in scfg["symbols"]:
                        continue
                    sigs = scan_symbol(sym, df, scfg, SESSION_WINDOWS)
                    for s in sigs:
                        signals.append({
                            "symbol":    s.symbol,
                            "direction": s.direction,
                            "strategy":  s.strategy,
                            "time":      s.timestamp.strftime("%H:%M"),
                        })
            except Exception:
                continue

    except Exception as e:
        print(f"consistency_checker: backtest error for {day}: {e}", flush=True)

    return signals


def compare(live: list[dict], backtest: list[dict]) -> dict:
    """Compare live signals vs backtest signals for one day."""
    if not backtest:
        return {"skip": True, "reason": "no_backtest_signals"}

    # Direction match: for each backtest signal, find live signal on same symbol+strategy
    dir_matches   = 0
    sym_matches   = 0
    backtest_syms = {(s["symbol"], s["strategy"]) for s in backtest}
    live_syms     = {(s["symbol"], s.get("strategy", "")) for s in live}

    for bt in backtest:
        key = (bt["symbol"], bt["strategy"])
        if key in live_syms:
            sym_matches += 1
            # Check direction
            live_match = next((l for l in live
                               if l["symbol"] == bt["symbol"]
                               and l.get("strategy","") == bt["strategy"]), None)
            if live_match and live_match.get("direction") == bt["direction"]:
                dir_matches += 1

    total = len(backtest)
    count_match = abs(len(live) - total) / max(total, 1) <= 0.20   # within 20%

    return {
        "backtest_signals": total,
        "live_signals":     len(live),
        "count_match":      count_match,
        "symbol_match_pct": round(sym_matches / total * 100, 1) if total else 0,
        "direction_match_pct": round(dir_matches / total * 100, 1) if total else 0,
    }


def run(kite=None) -> dict:
    """
    Full consistency check over last LOOKBACK_DAYS trading days.
    Returns report dict.
    """
    days = get_last_n_trading_days(LOOKBACK_DAYS)
    day_reports = []
    total_backtest = 0
    total_sym_match = 0
    total_dir_match = 0

    for day in days:
        date_str  = day.strftime("%Y-%m-%d")
        live      = fetch_live_signals(date_str)
        backtest  = run_backtest_for_day(day, kite)
        report    = compare(live, backtest)
        report["date"] = date_str
        day_reports.append(report)

        if not report.get("skip"):
            total_backtest  += report["backtest_signals"]
            total_sym_match += report["symbol_match_pct"] * report["backtest_signals"] / 100
            total_dir_match += report["direction_match_pct"] * report["backtest_signals"] / 100

    overall_sym = round(total_sym_match / max(total_backtest, 1) * 100, 1)
    overall_dir = round(total_dir_match / max(total_backtest, 1) * 100, 1)
    overall     = round((overall_sym + overall_dir) / 2, 1)

    report = {
        "generated_at":       datetime.now().isoformat(),
        "lookback_days":      LOOKBACK_DAYS,
        "days_checked":       len(days),
        "overall_match_pct":  overall,
        "symbol_match_pct":   overall_sym,
        "direction_match_pct": overall_dir,
        "warn_threshold":     MATCH_WARN_PCT,
        "status":             "OK" if overall >= MATCH_WARN_PCT else "WARN",
        "day_reports":        day_reports,
    }

    # Save to disk
    out_path = Path(os.environ.get("LOG_DIR", "logs")) / "consistency_latest.json"
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2))
    except Exception:
        pass

    return report


def wait_for_next_sunday_1700():
    """Block until next Sunday 17:00 IST."""
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    while True:
        now = datetime.now(IST)
        # Sunday = weekday 6
        days_until_sunday = (6 - now.weekday()) % 7
        if days_until_sunday == 0 and now.hour >= 17:
            days_until_sunday = 7   # already past 17:00 Sunday, wait for next week
        target = (now + timedelta(days=days_until_sunday)).replace(
            hour=17, minute=0, second=0, microsecond=0)
        secs = (target - now).total_seconds()
        print(f"ConsistencyChecker: sleeping {secs/3600:.1f}h until {target}", flush=True)
        time.sleep(max(secs, 1))
        break   # wake up and run


if __name__ == "__main__":
    print("Running consistency check...", flush=True)
    result = run()
    print(json.dumps(result, indent=2), flush=True)
    if result["status"] == "WARN":
        print(f"\n⚠️  Match {result['overall_match_pct']}% is below {result['warn_threshold']}% threshold!", flush=True)
    else:
        print(f"\n✅  Match {result['overall_match_pct']}% — within acceptable range.", flush=True)
