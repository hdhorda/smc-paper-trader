# A2 Deployment Checklist (paper trading)

**Strategy:** A2_OB60_CHOCH5 — 60min causal Order Block zone → 5min CHoCH trigger
**Validated:** 2025 PF 1.65 (5,278 trades) · 2026 OOS PF 2.48 · breakeven slippage ~0.11%/side
**Files changed:** `a2_strategy.py` (new) · `app.py` (import + STRATEGIES entry + dispatch)

## Pre-deploy (on Windows)
1. Verify `python -c "import a2_strategy"` runs clean inside `live_trading\` (needs `smc_backtest.py` one level up)
2. Optional replay test: feed a historical day through `scan_symbol_a2` (done 2026-07-03: reproduces backtest trade exactly — RELIANCE 2026-01-20 09:50 SHORT @1403.20)
3. `git add a2_strategy.py app.py DEPLOY_A2.md && git commit && git push`

## Deploy (on Oracle server)
```bash
cd /home/smcbot/smc-trader && git pull
sudo systemctl restart smc-trader
journalctl -u smc-trader -f    # watch startup: should list 3 strategies incl. A2_OB60_CHOCH5
```

## Watch after deploy
- **RAM**: 40 new symbols ≈ 67 total subscriptions on a 956MB box. Check `free -m` after warmup; if swapping, trim A2 symbols to top-25.
- **Warmup**: A2 needs ≥25 completed 60min bars → `WARMUP_BARS=3500` (~9 days) is sufficient ✓
- **First day**: expect roughly 2–5 A2 signals/day across the 40 names (backtest avg ~25/day across 213 → ~5/day on 40 liquid names)
- **EOD reconciliation**: `troubleshoot_brain.py eod` currently matches S4A/S5 only — extend to A2 (compare `paper_trades_*.csv` A2 rows against `arch2b_run.py --symbol X` same-day output) — TODO
- Sizing/costs come from `config.py`: POSITION_SIZE_RS=2,00,000 · SLIPPAGE_PCT=0.05/leg · CHARGES_PCT=0.07081 — matches the "realistic" sim scenario

## Pass / kill criteria (declared in advance)
- **Review at 8 weeks or 100 A2 trades**, whichever later
- **PASS** → consider live: net-profitable AND live PF ≥ 70% of backtest (≥ ~1.15 net) AND measured entry slippage ≤ 0.10%/side
- **KILL early** if: drawdown > 12% of notional (₹10L basis) · or 10+ consecutive losing days · or measured slippage > 0.10%/side sustained
- Expectation calibration (from portfolio sim, stocks-only, ₹2.5L cap): 0.05% slip → ~81% CAGR path; 0.10% slip → ~24% CAGR path. Paper measures which world we're in.

## Notes
- `a2_strategy.py` implements the CAUSAL OB (zone active only after its BOS bar). Do NOT swap in engine `detect_order_blocks` — it marks zones before their BOS (look-ahead; see vault: Backtest - Architecture 2).
- A2 ignores the S4A session windows by design (entries allowed 09:15–14:00, matching backtest).
- Index symbols must never be added to A2 (cash-untradeable; also see 213-vs-232 universe note).
