"""
options_selector.py — Phase 2 Options Strike Selector (DORMANT)
================================================================
⚠️  NOT ACTIVE during paper trading Phase 1.
    Enable when 60 days of cash-market paper signals show edge.

This module selects the appropriate NSE options contract (ATM or ITM)
for a given signal from the SMC paper trading engine.

Phase 2 activation checklist:
  □ 60+ trading days of paper results with win rate > 55%
  □ Strategy confidence interval calculated (not just point estimate)
  □ F&O eligible stock confirmed (see F_AND_O_ELIGIBLE below)
  □ Lot sizes verified in NSEFO_LOT_SIZES
  □ Options premium budget set (PREMIUM_BUDGET_RS env var)
  □ PAPER_ONLY guardrail updated to allow options orders (app.py)
  □ Zerodha Kite Connect subscription includes options data feed

Strike selection logic:
  1. Fetch nearest weekly/monthly expiry
  2. For LONG signal  → buy CE (Call) strike ATM or 1-strike ITM
  3. For SHORT signal → buy PE (Put) strike ATM or 1-strike ITM
  4. Filter: IV rank < 50 (avoid buying expensive options)
  5. Filter: open interest > 500 lots (liquidity check)
  6. Risk = premium paid × lot_size (not fixed ₹2L like cash)
  7. Exit: SL on premium = 50% loss; TP = 100% profit

Usage (Phase 2):
  from options_selector import select_strike, get_lot_size, is_fo_eligible
  if is_fo_eligible(signal.symbol):
      strike_info = select_strike(kite, signal)
      if strike_info:
          # place options order here
          pass
"""

import os
from datetime import datetime, date, timedelta
from typing import Optional

# ── F&O eligible NSE stocks (subset relevant to our watchlists) ────────────────
# Source: NSE F&O stock list (verify monthly — additions/deletions happen)
F_AND_O_ELIGIBLE = {
    # S4A watchlist
    "ABCAPITAL", "PFC", "RELIANCE", "BOSCHLTD", "JSWSTEEL", "IEX", "OIL",
    "LUPIN", "GODREJCP", "PAGEIND", "LODHA", "ALKEM", "DIXON", "JUBLFOOD",
    "ETERNAL", "DABUR", "BAJAJFINSV",
    # S5 watchlist
    "TRENT", "AMBER", "SAIL", "CHOLAFIN", "BANKBARODA", "PGEL", "BIOCON",
}

# ── NSE F&O lot sizes (verify at: https://www.nseindia.com/products-services/equity-derivatives-lot-size)
# Update these monthly — lot sizes change with new expiry series
NSEFO_LOT_SIZES = {
    "RELIANCE":   250,
    "BAJAJFINSV": 125,
    "JSWSTEEL":   1350,
    "PFC":        4500,
    "LUPIN":      500,
    "DIXON":      125,
    "PAGEIND":    15,
    "TRENT":      375,
    "BANKBARODA": 5850,
    "CHOLAFIN":   500,
    "BIOCON":     2700,
    "SAIL":       8550,
    "JUBLFOOD":   1250,
    "DABUR":      1250,
    "GODREJCP":   500,
    "IEX":        3750,
    "OIL":        2700,
    "ALKEM":      125,
    "LODHA":      1400,
    "ETERNAL":    1200,
    "GMRAIRPORT": 15000,
    "AMBER":      200,
    "PGEL":       600,
    "ABCAPITAL":  4500,
    "APLAPOLLO":  1250,
    "BOSCHLTD":   25,
    "FORCEMOT":   75,
}

PREMIUM_BUDGET_RS = int(os.environ.get("PREMIUM_BUDGET_RS", "5000"))  # max premium per options trade
ITM_STRIKES       = int(os.environ.get("OPTIONS_ITM_STRIKES", "1"))   # how many strikes ITM (0=ATM, 1=1-ITM)
IV_RANK_MAX       = float(os.environ.get("IV_RANK_MAX", "50"))         # don't buy if IV rank > this


def is_fo_eligible(symbol: str) -> bool:
    """Check if symbol is F&O eligible."""
    return symbol in F_AND_O_ELIGIBLE


def get_lot_size(symbol: str) -> int:
    """Return lot size for symbol. Returns 0 if not F&O eligible."""
    return NSEFO_LOT_SIZES.get(symbol, 0)


def get_nearest_expiry(kite, symbol: str) -> Optional[date]:
    """
    Fetch the nearest weekly/monthly expiry date for a symbol's options.
    Returns None on error.
    """
    # Phase 2 TODO: implement
    # expiry = kite.instruments("NFO") filtered by symbol + option_type
    raise NotImplementedError(
        "options_selector.get_nearest_expiry() is Phase 2 — not yet implemented.\n"
        "Activate after 60 days of validated cash-market paper trading."
    )


def select_strike(kite, signal, iv_check: bool = True) -> Optional[dict]:
    """
    Select the appropriate options strike for a signal.

    Args:
        kite:     KiteConnect instance
        signal:   Signal object from signal_engine
        iv_check: If True, skip if IV rank > IV_RANK_MAX

    Returns:
        dict with keys: tradingsymbol, strike, option_type, expiry,
                        lot_size, estimated_premium, max_loss_rs, target_rs
        or None if no suitable contract found.
    """
    # Phase 2 TODO: implement
    raise NotImplementedError(
        "options_selector.select_strike() is Phase 2 — not yet implemented.\n"
        "\nPhase 2 activation checklist:\n"
        "  1. 60+ days paper trading with win rate > 55%\n"
        "  2. F&O eligible check: is_fo_eligible(symbol)\n"
        "  3. Lot size: get_lot_size(symbol)\n"
        "  4. Expiry: get_nearest_expiry(kite, symbol)\n"
        "  5. ATM/ITM strike based on signal.entry_price\n"
        "  6. IV rank filter (avoid buying expensive options)\n"
        "  7. OI filter (>500 lots for liquidity)\n"
        "  8. Premium ≤ PREMIUM_BUDGET_RS / lot_size"
    )


def estimate_options_pnl(
    entry_premium: float,
    exit_premium:  float,
    lot_size:      int,
    option_type:   str,   # 'CE' or 'PE'
) -> dict:
    """
    Calculate options P&L.
    Note: Options P&L is not the same as cash P&L — premium decay affects hold time.
    """
    pnl_per_lot = (exit_premium - entry_premium) * lot_size
    pnl_pct     = (exit_premium - entry_premium) / entry_premium * 100 if entry_premium else 0
    return {
        "entry_premium":  entry_premium,
        "exit_premium":   exit_premium,
        "pnl_per_lot_rs": round(pnl_per_lot, 2),
        "pnl_pct":        round(pnl_pct, 1),
        "lot_size":       lot_size,
        "option_type":    option_type,
    }


# ── Phase 2 integration notes ──────────────────────────────────────────────────
"""
When ready to activate Phase 2:

1. In app.py, after a signal fires and position is NOT open:
   from options_selector import is_fo_eligible, select_strike
   if is_fo_eligible(sig.symbol) and USE_OPTIONS:
       strike_info = select_strike(kite, sig)
       if strike_info:
           # place CE/PE order via Kite
           # track as paper options position

2. Position sizing: instead of POSITION_SIZE_RS fixed lot,
   use: premium × lot_size (capped at PREMIUM_BUDGET_RS)

3. Exit logic: SL = premium × 0.5 (50% premium loss stops)
               TP = premium × 2.0 (100% profit target)

4. Remove PAPER_ONLY guardrail from modify_order/cancel_order
   ONLY for options orders, after extensive paper validation.
"""
