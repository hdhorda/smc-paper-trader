# Gap-Down Bounce — Deployment Checklist (paper)

**Rule frozen 2026-07-04** — live paper is the genuine out-of-sample test. Do not tune.
Validated: net PF 1.80, +0.62%/trade, 7/7 years positive · see vault note.
Replay-validated: 2026-06-19 IT panic → 10/10 winners, basket +2.51%.

## Files
- `gap_scanner.py` — standalone (REST quotes only; no WebSocket, independent of smc-trader)
- `gap_universe.txt` — 213 stocks (indices excluded)

## Deploy (Oracle)
```bash
cd /home/smcbot/smc-trader && git pull
# uses existing .env (KITE_API_KEY, KITE_ACCESS_TOKEN, TG_TOKEN, TG_CHAT)
crontab -e   # add (server runs IST):
14 9  * * 1-5  cd /home/smcbot/smc-trader && python3 gap_scanner.py --scan >> /home/ubuntu/logs/gap_scanner.log 2>&1
16 15 * * 1-5  cd /home/smcbot/smc-trader && python3 gap_scanner.py --eod  >> /home/ubuntu/logs/gap_scanner.log 2>&1
```
Depends on the same daily Kite token rotation as smc-trader.

## What to expect
- Most days: one Telegram at ~09:16 — "N signals, outside 8-30 band, NO TRADE" (that's correct behaviour; ~16 trade days/yr)
- Stress days: entry alert 09:16, fills note 09:20, EOD summary 15:16
- Logs: `logs/gap_trades.csv` (results), `logs/gap_positions_YYYY-MM-DD.json` (intraday state)

## Review gate (pre-declared)
After 8-10 traded stress days: net-positive AND avg/trade ≥ +0.3% gross → real-money discussion.
Kill: 3 consecutive stress days with basket < −1%, or measured entry slippage > 0.15%/side sustained.

## Known caveats
- Ex-dividend gaps inside the −2..−8% band are counted (they were in the backtest too — consistency preserved). Post-paper improvement: skip ex-date stocks.
- Offline validation any time: `python3 gap_scanner.py --replay YYYY-MM-DD` (on Windows with Historicalcash present).
