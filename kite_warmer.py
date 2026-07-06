"""
kite_warmer.py
==============
Replaces local parquet warmup with Kite historical API.
Fetches the last WARMUP_BARS of 1min data per symbol from Kite.
No local files needed — works entirely from cloud.
"""
import os
import pandas as pd
from datetime import datetime, timedelta


WARMUP_BARS = int(os.environ.get("WARMUP_BARS", "2000"))  # ~5 trading days; covers 60min HTF (33+ candles)


def warmup_from_kite(kite, symbols: list[str], bar_window, max_retries: int = 3) -> dict[str, bool]:
    """
    Fetch last WARMUP_BARS 1min bars per symbol from Kite historical API.
    Seeds the bar_window with this data.
    Returns {symbol: True/False} — True if successfully warmed up.
    Retries up to max_retries times on failure.
    """
    import time
    results = {}
    # 2000 bars @ 375 bars/day ≈ 5.3 trading days; use 14 calendar days for safety (covers weekends/holidays)
    from_dt = datetime.now() - timedelta(days=14)
    to_dt   = datetime.now()

    # Cache instrument list once for the whole batch
    try:
        instruments = kite.instruments("NSE")
        inst_map = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}
    except Exception as e:
        print(f"  ✗ Could not fetch instrument list: {e}", flush=True)
        return {s: False for s in symbols}

    for sym in symbols:
        success = False
        for attempt in range(1, max_retries + 1):
            try:
                instrument_token = inst_map.get(sym)
                if not instrument_token:
                    print(f"  ✗ {sym}: instrument not found", flush=True)
                    break

                candles = kite.historical_data(
                    instrument_token,
                    from_date=from_dt,
                    to_date=to_dt,
                    interval="minute",
                    continuous=False,
                    oi=False,
                )
                if not candles:
                    print(f"  ✗ {sym}: no candles returned (attempt {attempt})", flush=True)
                    time.sleep(5)
                    continue

                df = pd.DataFrame(candles)
                df = df.rename(columns={
                    "date": "ts", "open": "open", "high": "high",
                    "low": "low", "close": "close", "volume": "volume",
                })
                df["ts"] = pd.to_datetime(df["ts"])
                df = df[["ts","open","high","low","close","volume"]].tail(WARMUP_BARS).copy()
                df["tradingsymbol"] = sym
                df["date"]          = df["ts"].dt.date

                bar_window.seed(sym, df)
                print(f"  ✓ {sym}: {len(df)} bars from Kite", flush=True)
                success = True
                break

            except Exception as e:
                print(f"  ✗ {sym} (attempt {attempt}/{max_retries}): {e}", flush=True)
                if attempt < max_retries:
                    time.sleep(10 * attempt)   # 10s, 20s backoff

        results[sym] = success

    return results


def _get_token(kite, symbol: str) -> int | None:
    """Look up NSE instrument token for a symbol (single lookup, use inst_map in bulk ops)."""
    try:
        instruments = kite.instruments("NSE")
        for inst in instruments:
            if inst["tradingsymbol"] == symbol:
                return inst["instrument_token"]
    except Exception:
        pass
    return None


def get_kite_client():
    """Build authenticated KiteConnect client from environment variables."""
    from kiteconnect import KiteConnect
    api_key      = os.environ["KITE_API_KEY"]
    access_token = os.environ["KITE_ACCESS_TOKEN"]
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def refresh_access_token(request_token: str) -> str:
    """
    Exchange a request_token (from Kite login redirect) for an access_token.
    Call this once per day after the user logs in via /auth/callback.
    """
    from kiteconnect import KiteConnect
    import hashlib
    api_key    = os.environ["KITE_API_KEY"]
    api_secret = os.environ["KITE_API_SECRET"]
    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    return data["access_token"]
