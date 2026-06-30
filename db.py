"""
db.py — SQLite persistence for paper trades and signals
All data survives restarts. Query with any SQLite tool or pandas.
"""
import sqlite3
import os
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "paper_trades.db")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fired_at    TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            strategy    TEXT NOT NULL,
            direction   TEXT NOT NULL,
            entry_price REAL,
            sl_price    REAL,
            tp_price    REAL,
            entry_tf    INTEGER,
            htf_signal  TEXT,
            fvg_top     REAL,
            fvg_bottom  REAL,
            pd_zone     TEXT
        );

        CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol       TEXT NOT NULL,
            strategy     TEXT NOT NULL,
            direction    TEXT NOT NULL,
            entry_time   TEXT NOT NULL,
            exit_time    TEXT,
            entry_price  REAL,
            exit_price   REAL,
            exit_reason  TEXT,
            pnl_pts      REAL,
            pnl_pct      REAL,
            pnl_rs       REAL,
            win          INTEGER,
            entry_tf     INTEGER,
            pd_zone      TEXT,
            htf_signal   TEXT,
            status       TEXT DEFAULT 'open'
        );

        CREATE INDEX IF NOT EXISTS idx_trades_symbol   ON trades(symbol);
        CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
        CREATE INDEX IF NOT EXISTS idx_trades_entry    ON trades(entry_time);
        """)


def insert_signal(sig_dict: dict):
    with _conn() as c:
        c.execute("""
            INSERT INTO signals
            (fired_at,symbol,strategy,direction,entry_price,sl_price,tp_price,
             entry_tf,htf_signal,fvg_top,fvg_bottom,pd_zone)
            VALUES (:fired_at,:symbol,:strategy,:direction,:entry_price,:sl_price,
                    :tp_price,:entry_tf,:htf_signal,:fvg_top,:fvg_bottom,:pd_zone)
        """, sig_dict)
        return c.lastrowid


def insert_trade(trade_dict: dict) -> int:
    """Insert an open trade. Returns row id."""
    with _conn() as c:
        c.execute("""
            INSERT INTO trades
            (symbol,strategy,direction,entry_time,entry_price,sl_price,tp_price,
             entry_tf,pd_zone,htf_signal,status)
            VALUES (:symbol,:strategy,:direction,:entry_time,:entry_price,:sl_price,
                    :tp_price,:entry_tf,:pd_zone,:htf_signal,'open')
        """, trade_dict)
        return c.lastrowid


def close_trade(trade_id: int, exit_dict: dict):
    """Mark trade as closed with exit details."""
    with _conn() as c:
        c.execute("""
            UPDATE trades SET
                exit_time=:exit_time, exit_price=:exit_price, exit_reason=:exit_reason,
                pnl_pts=:pnl_pts, pnl_pct=:pnl_pct, pnl_rs=:pnl_rs,
                win=:win, status='closed'
            WHERE id=:id
        """, {**exit_dict, "id": trade_id})


def get_open_trades() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY entry_time"
        ).fetchall()]


def get_closed_trades(limit: int = 100, strategy: str = None) -> list[dict]:
    with _conn() as c:
        if strategy:
            rows = c.execute(
                "SELECT * FROM trades WHERE status='closed' AND strategy=? ORDER BY exit_time DESC LIMIT ?",
                (strategy, limit)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM trades WHERE status='closed' ORDER BY exit_time DESC LIMIT ?",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_daily_stats(date_str: str = None) -> dict:
    """Stats for a specific date (default: today)."""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    with _conn() as c:
        rows = c.execute("""
            SELECT COUNT(*) as trades, SUM(win) as wins,
                   SUM(pnl_rs) as net_pnl
            FROM trades
            WHERE status='closed' AND exit_time LIKE ?
        """, (f"{date_str}%",)).fetchone()
        t = rows["trades"] or 0
        w = rows["wins"] or 0
        pnl = rows["net_pnl"] or 0.0
        return {
            "date":    date_str,
            "trades":  t,
            "wins":    w,
            "wr":      round(w / t * 100, 1) if t > 0 else 0.0,
            "net_pnl": round(pnl, 0),
        }


def get_strategy_summary() -> list[dict]:
    """Lifetime stats per strategy."""
    with _conn() as c:
        rows = c.execute("""
            SELECT strategy,
                   COUNT(*) as trades, SUM(win) as wins,
                   ROUND(SUM(pnl_rs),0) as net_pnl,
                   ROUND(AVG(pnl_rs),0) as avg_pnl_per_trade
            FROM trades WHERE status='closed'
            GROUP BY strategy ORDER BY net_pnl DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_recent_signals(limit: int = 20) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM signals ORDER BY fired_at DESC LIMIT ?", (limit,)
        ).fetchall()]
