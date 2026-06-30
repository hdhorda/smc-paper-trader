"""
tick_recorder.py — Background live tick recorder to daily parquet files
========================================================================
Records every live tick (symbol, timestamp, last_price, volume) to:
  logs/ticks_YYYY-MM-DD.parquet

Why bother:
  ~50 MB/day for 213 stocks.  After 3 months you have real NSE
  microstructure data for strategy R&D — more valuable than any
  paid data subscription once you have 60+ days accumulated.

Usage (wire into app.py on_ticks handler):
  from tick_recorder import TickRecorder
  _tick_recorder = TickRecorder()          # create once at startup
  _tick_recorder.record(sym, ltp, volume, ts)  # call inside on_ticks

The recorder buffers ticks in RAM and flushes to parquet every
FLUSH_INTERVAL_SECS (default 60s) on a daemon thread.
Old files are auto-purged after KEEP_DAYS (default 90).
"""

import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOG_DIR           = Path(os.environ.get("LOG_DIR", "logs"))
FLUSH_INTERVAL_S  = int(os.environ.get("TICK_FLUSH_SECS",  "60"))
KEEP_DAYS         = int(os.environ.get("TICK_KEEP_DAYS",   "90"))


class TickRecorder:
    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._buf: list[dict] = []          # in-memory buffer
        self._lock = threading.Lock()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="tick-recorder"
        )
        self._flush_thread.start()
        self._purge_old()
        print("TickRecorder started — ticks → logs/ticks_YYYY-MM-DD.parquet", flush=True)

    def record(self, symbol: str, price: float, volume: int, ts: datetime):
        """Call this for every tick inside on_ticks. Thread-safe."""
        with self._lock:
            self._buf.append({
                "ts":     ts.isoformat(),
                "symbol": symbol,
                "price":  price,
                "volume": volume,
            })

    def _flush_loop(self):
        while True:
            time.sleep(FLUSH_INTERVAL_S)
            self._flush()

    def _flush(self):
        with self._lock:
            if not self._buf:
                return
            batch = self._buf.copy()
            self._buf.clear()

        try:
            import pandas as pd
            df_new = pd.DataFrame(batch)
            df_new["ts"] = pd.to_datetime(df_new["ts"])

            today_str  = datetime.now().strftime("%Y-%m-%d")
            out_path   = LOG_DIR / f"ticks_{today_str}.parquet"

            if out_path.exists():
                df_old = pd.read_parquet(out_path)
                df_out = pd.concat([df_old, df_new], ignore_index=True)
            else:
                df_out = df_new

            df_out.to_parquet(out_path, index=False, compression="snappy")
        except Exception as e:
            print(f"TickRecorder flush error: {e}", flush=True)

    def _purge_old(self):
        """Delete tick files older than KEEP_DAYS on startup."""
        cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
        for f in LOG_DIR.glob("ticks_*.parquet"):
            try:
                file_date = datetime.strptime(f.stem.replace("ticks_", ""), "%Y-%m-%d")
                if file_date < cutoff:
                    f.unlink()
                    print(f"TickRecorder: purged {f.name}", flush=True)
            except Exception:
                pass

    def stats(self) -> dict:
        """How many ticks buffered and files on disk."""
        files = sorted(LOG_DIR.glob("ticks_*.parquet"))
        total_mb = sum(f.stat().st_size for f in files) / 1_048_576
        return {
            "buffered_ticks": len(self._buf),
            "parquet_files":  len(files),
            "total_mb":       round(total_mb, 1),
            "oldest_file":    files[0].name if files else None,
            "newest_file":    files[-1].name if files else None,
        }
