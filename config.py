"""
Live Trading Configuration
==========================
Edit this file before running. Set MOCK_MODE=True to test without Kite API.
"""

# ── Kite Connect credentials ──────────────────────────────────────────────────
# Get from: https://kite.trade/  (Rs 2000/month subscription)
API_KEY    = "your_api_key_here"
API_SECRET = "your_api_secret_here"
ACCESS_TOKEN = ""   # generated daily via kite.generate_session() — see README

# ── Mode ──────────────────────────────────────────────────────────────────────
MOCK_MODE = True   # True = replay historical parquets (no API needed)
                   # False = live Kite WebSocket feed

# ── Strategy selection ────────────────────────────────────────────────────────
STRATEGIES = {
    "S4A": {
        "enabled": True,
        "description": "S1 + Session Filter (CHoCH + FVG + LiqGrab + PD)",
        "timeframes": [1, 3, 5, 15],   # entry TFs to scan
        "htf_signal": None,            # no HTF filter for S4-A
        "symbols": [                   # WL20 watchlist
            "ABCAPITAL", "APLAPOLLO", "PFC",        "RELIANCE",
            "BOSCHLTD",  "JSWSTEEL",  "IEX",        "OIL",
            "LUPIN",     "GODREJCP",  "PAGEIND",    "LODHA",
            "ALKEM",     "DIXON",     "JUBLFOOD",   "ETERNAL",
            "GMRAIRPORT","DABUR",     "BAJAJFINSV", "FORCEMOT",
        ],
    },
    "S5_OB60_5": {
        "enabled": True,
        "description": "S5: 1H OB context + 5min FVG entry (top 7 stocks)",
        "timeframes": [5],
        "htf_signal": "ob",
        "htf_tf": 60,
        "symbols": [
            "TRENT", "AMBER", "SAIL", "CHOLAFIN", "BANKBARODA", "PGEL", "BIOCON",
        ],
    },
}

# ── Risk / position sizing ────────────────────────────────────────────────────
POSITION_SIZE_RS  = 200_000   # Rs per trade (same as backtest)
CHARGES_PCT       = 0.07081   # % per round-trip (brokerage + STT + GST)
SLIPPAGE_PCT      = 0.05      # % slippage per leg (buy high / sell low) — realistic for NSE mid-caps

# ── Session windows (IST) ─────────────────────────────────────────────────────
SESSION_WINDOWS = [
    ("09:15", "11:30"),
    ("13:30", "15:15"),
]

# ── Data paths ────────────────────────────────────────────────────────────────
import os
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
PARQUET_DIR  = r"C:\BreezeProjects\Price Action\NSE_1min_parquets"  # adjust if different
LOG_DIR      = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ── Rolling window size ───────────────────────────────────────────────────────
WARMUP_BARS = 500   # 1min bars loaded on startup per symbol
