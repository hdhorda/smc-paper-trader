# SMC Paper Trading System — NSE Equities

![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python)
![NSE](https://img.shields.io/badge/Exchange-NSE%20India-orange)
![Status](https://img.shields.io/badge/Status-Paper%20Trading-yellow)
![Cloud](https://img.shields.io/badge/Cloud-Oracle%20Free%20Tier-red?logo=oracle)
![License](https://img.shields.io/badge/License-MIT-green)

An end-to-end automated **Smart Money Concepts (SMC/ICT)** paper trading system for NSE equities. Built from backtest → signal engine → live data pipeline → cloud deployment. No local setup needed — runs entirely on Oracle Cloud Always Free.

> **PAPER TRADING ONLY.** `KiteConnect.place_order` is monkey-patched to raise `RuntimeError` at startup. No real orders are ever placed.

---

## What It Does

1. **Backtested** 9 SMC/ICT strategy variants across 232 NSE stocks (18 months of 1-min data)
2. **Selected** 2 strategies with net profit factor > 1.5 and win rate > 55%
3. **Deployed** live on Oracle Cloud with Kite Connect WebSocket for real-time 1-min bars
4. **Tracks** virtual paper trades with SL/TP/EOD exits, real-time dashboard, and Telegram alerts
5. **Validates** live signal quality weekly against backtest replay (consistency checker)

---

## Strategies

| ID | Description | Universe | Entry TF | HTF | Backtest WR |
|----|-------------|----------|----------|-----|-------------|
| **S4A** | FVG + Liquidity Grab + CHoCH + PD Array + Session Filter | 20 NSE stocks (WL20) | 3/5/15 min | None | ~58% |
| **S5_OB60_5** | 1-Hour Order Block → 5min FVG entry | 7 NSE stocks (top S5) | 5 min | 60min OB | ~63% |

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Zerodha Kite Connect                         │
│   Historical API ──► Warmup (2000 bars per symbol @ 09:00)     │
│   WebSocket      ──► Live tick stream (09:15–15:30 IST)         │
└────────────────────────────┬────────────────────────────────────┘
                             │  1-min bar accumulation
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Data Guardian                               │
│   Gap Detection → Kite API Backfill → Retroactive SL/TP        │
│   Stale Data Detection → Circuit Breaker → Tick Rate Monitor    │
└────────────────────────────┬────────────────────────────────────┘
                             │  Clean bar stream
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              Signal Engine  (per symbol, parallel)              │
│   detect_swings → detect_fvgs → detect_choch → detect_ob       │
│   Session filter → PD zone → HTF confluence → Signal object     │
│   ThreadPoolExecutor(4 workers) — 213 stocks in ~1.6s           │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Risk Guardrails                              │
│   Max 15 concurrent positions │ Daily loss cap ₹50,000          │
│   Signal expiry (3 bars / 0.3% drift) │ Direction conflict skip │
│   Slippage simulation (0.05% per leg) │ Dedup cooldown 5 min    │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Paper Tracker                                │
│   Virtual positions: SL/TP/EOD exits                           │
│   P&L = (price_change × ₹2L position) – 0.07% brokerage        │
│   Hard close all positions at 15:00 IST daily                  │
└─────────┬───────────────────────────────────┬───────────────────┘
          │                                   │
          ▼                                   ▼
  SQLite DB (WAL mode)              Telegram Alerts
  JSONL Event Log Book              Signal fired / Exit / EOD summary
  Parquet Tick Archive              Token expired / WS disconnect
```

---

## Live Dashboard

A real-time Flask dashboard at `http://SERVER_IP:5001`:

- Open positions with entry/SL/TP and strategy
- Closed trades with P&L, exit reason, win/loss badge
- Strategy performance comparison (all-time)
- Signal log with direction and setup details
- Data quality panel: per-symbol health (GREEN/YELLOW/RED), ticks/min, gap count, circuit breaker status
- System log book with filter (Signals / Errors / WS Disconnects / Heartbeats)
- Token and WebSocket status badges

---

## Auto-Heal Features

| Feature | Implementation |
|---------|----------------|
| WebSocket reconnect | Exponential backoff: 5→10→20→40→60s |
| Token expiry handling | Pauses engine, resumes after `/auth` refresh |
| Gap detection + backfill | Fetches missing bars from Kite historical API |
| Retroactive SL/TP on gap | Checks if SL/TP was hit during gap bars |
| Stale data detection | Suppresses signals if no tick for 120s |
| Circuit breaker detection | Detects frozen price (NSE upper/lower circuit) |
| Signal expiry | Cancels entry if price drifts >0.3% or >3 bars |
| Daily dedup reset | Clears cooldown cache at 09:15 each morning |
| Oracle VM keepalive | Cron hits `/api/health` every 30 min |
| systemd supervisor | Auto-restarts on crash (10s, max 5/min) |
| Weekly consistency check | Re-runs backtest Sunday 17:00, alerts if <85% match |

---

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Live dashboard HTML |
| `GET /api/state` | Full system snapshot (positions, stats, calendar) |
| `GET /api/trades` | All closed trades (JSON) |
| `GET /api/signals?limit=50` | Recent signals |
| `GET /api/stats` | Daily P&L, strategy breakdown, scan lag metrics |
| `GET /api/logs?event=SIGNAL_FIRED` | Filterable event log |
| `GET /api/health?detail=1` | Health + data quality + tick recorder stats |
| `GET /auth` | Kite login redirect (refresh token daily before 09:15) |
| `GET /auth/callback` | Exchange request_token → access_token |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Live data | Zerodha Kite Connect WebSocket + Historical API |
| Signal detection | Custom SMC/ICT Python (FVG, CHoCH, BOS, OB, Liq Grab) |
| Parallel scan | `ThreadPoolExecutor` (4 workers) |
| Server | Flask + Gunicorn |
| Database | SQLite (WAL mode) → PostgreSQL for Phase 2 |
| Tick archive | Apache Parquet (~50MB/day for 213 stocks) |
| Alerts | Telegram Bot API |
| Cloud | Oracle Cloud Free Tier (ap-Mumbai-1, 4 OCPU / 24GB RAM) |
| Process supervisor | systemd |
| Calendar | NSE holidays 2025–2026, IST session phases |

---

## File Structure

```
live_trading/
├── app.py                  # Main Flask server + live engine + all threads
├── signal_engine.py        # SMC detection → Signal objects
├── paper_tracker.py        # Virtual positions, P&L calculation (with slippage)
├── data_guardian.py        # Gap detection, backfill, stale/circuit breaker
├── market_calendar.py      # NSE holidays, session phases, entry gate
├── kite_warmer.py          # Historical data warmup at 09:00
├── event_logger.py         # Structured JSONL log book + ring buffer
├── telegram_notifier.py    # Real-time Telegram alerts
├── tick_recorder.py        # Background parquet tick archive
├── consistency_checker.py  # Weekly live-vs-backtest signal comparison
├── options_selector.py     # Phase 2: ATM/ITM strike selection (dormant)
├── db.py                   # SQLite schema + query layer
├── live_runner.py          # BarWindow rolling bar buffer
├── config.py               # Strategy config, position sizing, risk params
├── live_dashboard.html     # Real-time dashboard UI
└── deploy/
    ├── oracle_setup.sh     # One-time Oracle Cloud server setup
    ├── smc-trader.service  # systemd service file
    └── ORACLE_DEPLOY.md    # Step-by-step deployment guide
```

---

## Adding a New Strategy

1. Create `Strategy_N/` folder under `Project_8/` with backtest results
2. Run backtest with `smc_backtest.py` — achieve WR > 55% before deploying
3. In `app.py`, add to `STRATEGIES` dict:
   ```python
   "S6_NEW": {
       "enabled": True,
       "description": "Your strategy description",
       "timeframes": [5, 15],
       "htf_signal": None,
       "symbols": ["SYM1", "SYM2", ...],
   }
   ```
4. No other changes needed — signal engine, tracker, dashboard, alerts all auto-adapt

---

## Daily Routine

| Time | Action |
|------|--------|
| 09:00 | Visit `http://SERVER_IP:5001/auth` → log in with Kite |
| 09:10 | Dashboard: Token OK + WS Connected confirmed |
| 09:15 | Engine scans automatically — Telegram alert on first signal |
| 15:00 | All positions hard-closed, EOD summary sent to Telegram |
| Sunday 17:00 | Weekly consistency check runs automatically |

---

## Paper Trading → Real Money Checklist

- [ ] 60+ trading days of paper results
- [ ] Win rate consistently > 55% across both strategies
- [ ] Weekly consistency checker shows > 85% match (live vs backtest)
- [ ] Max drawdown tested across multiple volatile days
- [ ] Oracle VM upgraded to paid shape (VM.Standard3.Flex)
- [ ] SQLite migrated to PostgreSQL
- [ ] Dashboard basic auth enabled
- [ ] Options strike selector implemented (see `options_selector.py`)
- [ ] Kite Connect subscription includes F&O data feed

---

## Showcase & Contact

This system was built as a proof-of-concept for automated NSE equity trading using SMC/ICT methodology. The same architecture can be adapted for:

- Different strategy logic (RSI, VWAP, volume profile, momentum)
- Different exchanges (BSE, MCX, crypto)
- Expanded watchlists (Nifty 500, sector-specific)
- Options trading (Phase 2 skeleton already included)

For custom builds or collaboration: **hardik.dhorda@gmail.com**

---

*Built with Python 3.11 · Zerodha Kite Connect · Oracle Cloud Free Tier · NSE India*
