"""
market_calendar.py — NSE Trading Calendar & Session Rules
==========================================================
Single source of truth for all time-based decisions in the live engine.

Rules (as configured):
  - Trading days   : Monday–Friday, excluding NSE holidays
  - Market open    : 09:15 – 15:30 (WebSocket active, positions monitored)
  - Entry allowed  : 09:15 – 15:00 (no new positions in last 30 min)
  - EOD exit time  : 15:00 (hard close all open positions, no new entries)
  - Pre-market     : 09:00 – 09:15 (warmup window, no trading)

Usage:
  from market_calendar import calendar
  if not calendar.is_trading_day():   # skip non-trading days
  if not calendar.is_entry_allowed(): # block new entries after 15:00
  if calendar.is_eod_exit_time():     # trigger hard close at 15:00
"""

import os
from datetime import date, time, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ── NSE Holidays 2025 & 2026 ──────────────────────────────────────────────────
# Source: NSE India official holiday calendar
# Add new years as needed — or override via env var NSE_EXTRA_HOLIDAYS=YYYY-MM-DD,...

_NSE_HOLIDAYS: set[date] = {
    # ── 2025 ──
    date(2025,  2, 26),   # Mahashivratri
    date(2025,  3, 14),   # Holi
    date(2025,  3, 31),   # Id-Ul-Fitr (Ramzan Eid)
    date(2025,  4, 10),   # Shri Ram Navami
    date(2025,  4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2025,  4, 18),   # Good Friday
    date(2025,  5,  1),   # Maharashtra Day
    date(2025,  8, 15),   # Independence Day
    date(2025,  8, 27),   # Ganesh Chaturthi
    date(2025, 10,  2),   # Gandhi Jayanti / Dussehra
    date(2025, 10, 21),   # Diwali Laxmi Pujan (Muhurat Trading day — special)
    date(2025, 10, 22),   # Diwali Balipratipada
    date(2025, 11,  5),   # Prakash Gurpurb Sri Guru Nanak Dev Ji
    date(2025, 12, 25),   # Christmas

    # ── 2026 ──
    date(2026,  1, 26),   # Republic Day
    date(2026,  2, 26),   # Mahashivratri (tentative)
    date(2026,  3,  3),   # Holi (tentative)
    date(2026,  3, 20),   # Id-Ul-Fitr / Ramzan Eid (tentative)
    date(2026,  3, 30),   # Shri Ram Navami (tentative)
    date(2026,  4,  3),   # Good Friday (tentative)
    date(2026,  4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026,  5,  1),   # Maharashtra Day
    date(2026,  8, 15),   # Independence Day
    date(2026,  9, 17),   # Ganesh Chaturthi (tentative)
    date(2026, 10,  2),   # Gandhi Jayanti
    date(2026, 11, 10),   # Diwali Laxmi Pujan (tentative)
    date(2026, 11, 25),   # Guru Nanak Jayanti (tentative)
    date(2026, 12, 25),   # Christmas
}

# Allow override / additions via environment variable
_extra = os.environ.get("NSE_EXTRA_HOLIDAYS", "")
for _d in _extra.split(","):
    _d = _d.strip()
    if _d:
        try:
            _NSE_HOLIDAYS.add(date.fromisoformat(_d))
        except ValueError:
            pass


class MarketCalendar:
    """
    All session timing logic in one place.
    All times are in IST (Asia/Kolkata).
    """

    # Configurable via env vars
    MARKET_OPEN   = time(9, 15)
    MARKET_CLOSE  = time(15, 30)
    ENTRY_CUTOFF  = time(15,  0)   # no new entries after this
    EOD_EXIT_TIME = time(15,  0)   # hard close all positions at this time
    WARMUP_START  = time(9,   0)   # start Kite warmup at this time

    def _now(self) -> datetime:
        return datetime.now(tz=IST)

    def _today(self) -> date:
        return self._now().date()

    # ── Day-level checks ───────────────────────────────────────────────────────

    def is_trading_day(self, d: date = None) -> bool:
        """True if d (default: today) is a weekday and not an NSE holiday."""
        d = d or self._today()
        if d.weekday() >= 5:          # Saturday=5, Sunday=6
            return False
        return d not in _NSE_HOLIDAYS

    def next_trading_day(self) -> date:
        """Return the next calendar trading day after today."""
        from datetime import timedelta
        d = self._today() + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def days_until_next_trading_day(self) -> int:
        from datetime import timedelta
        d = self._today() + timedelta(days=1)
        count = 1
        while not self.is_trading_day(d):
            d += timedelta(days=1)
            count += 1
        return count

    # ── Intraday checks ────────────────────────────────────────────────────────

    def is_market_open(self) -> bool:
        """True during 09:15–15:30 on a trading day. WebSocket should be active."""
        if not self.is_trading_day():
            return False
        t = self._now().time().replace(tzinfo=None)
        return self.MARKET_OPEN <= t <= self.MARKET_CLOSE

    def is_entry_allowed(self) -> bool:
        """
        True only when new position entries are permitted.
        Entries blocked after 15:00 and on non-trading days.
        """
        if not self.is_trading_day():
            return False
        t = self._now().time().replace(tzinfo=None)
        return self.MARKET_OPEN <= t < self.ENTRY_CUTOFF

    def is_eod_exit_window(self) -> bool:
        """
        True between 15:00 and 15:30 — trigger hard close of all open positions.
        The engine calls this every bar cycle; first True triggers the mass exit.
        """
        if not self.is_trading_day():
            return False
        t = self._now().time().replace(tzinfo=None)
        return self.EOD_EXIT_TIME <= t <= self.MARKET_CLOSE

    def is_warmup_window(self) -> bool:
        """True between 09:00 and 09:15 — warm up bar window before market opens."""
        if not self.is_trading_day():
            return False
        t = self._now().time().replace(tzinfo=None)
        return self.WARMUP_START <= t < self.MARKET_OPEN

    def is_pre_market(self) -> bool:
        """True before 09:00 on a trading day."""
        if not self.is_trading_day():
            return False
        return self._now().time().replace(tzinfo=None) < self.WARMUP_START

    def seconds_to_open(self) -> int:
        """Seconds until next market open (09:15 on next trading day)."""
        from datetime import timedelta
        now = self._now()
        t = now.time().replace(tzinfo=None)
        if self.is_trading_day() and t < self.MARKET_OPEN:
            # Same day
            open_dt = now.replace(
                hour=self.MARKET_OPEN.hour,
                minute=self.MARKET_OPEN.minute,
                second=0, microsecond=0
            )
            return max(0, int((open_dt - now).total_seconds()))
        else:
            # Next trading day
            nd = self.next_trading_day()
            open_dt = datetime(nd.year, nd.month, nd.day,
                               self.MARKET_OPEN.hour, self.MARKET_OPEN.minute,
                               tzinfo=IST)
            return max(0, int((open_dt - now).total_seconds()))

    # ── Status summary ─────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Full status dict for /api/health and dashboard."""
        now      = self._now()
        today    = self._today()
        t        = now.time().replace(tzinfo=None)
        is_hol   = today in _NSE_HOLIDAYS

        if not self.is_trading_day():
            phase = "HOLIDAY" if is_hol else "WEEKEND"
        elif t < self.WARMUP_START:
            phase = "PRE_MARKET"
        elif t < self.MARKET_OPEN:
            phase = "WARMUP"
        elif t < self.EOD_EXIT_TIME:
            phase = "TRADING"
        elif t <= self.MARKET_CLOSE:
            phase = "EOD_EXIT"
        else:
            phase = "POST_MARKET"

        return {
            "date":             today.isoformat(),
            "time_ist":         now.strftime("%H:%M:%S"),
            "phase":            phase,
            "is_trading_day":   self.is_trading_day(),
            "is_holiday":       is_hol,
            "market_open":      self.is_market_open(),
            "entry_allowed":    self.is_entry_allowed(),
            "eod_exit_window":  self.is_eod_exit_window(),
            "next_trading_day": self.next_trading_day().isoformat(),
            "secs_to_open":     self.seconds_to_open(),
        }

    def holiday_name(self, d: date = None) -> str | None:
        """Return holiday name if d is a holiday (best-effort lookup)."""
        d = d or self._today()
        _names = {
            date(2025,  2, 26): "Mahashivratri",
            date(2025,  3, 14): "Holi",
            date(2025,  3, 31): "Id-Ul-Fitr",
            date(2025,  4, 10): "Ram Navami",
            date(2025,  4, 14): "Ambedkar Jayanti",
            date(2025,  4, 18): "Good Friday",
            date(2025,  5,  1): "Maharashtra Day",
            date(2025,  8, 15): "Independence Day",
            date(2025,  8, 27): "Ganesh Chaturthi",
            date(2025, 10,  2): "Gandhi Jayanti",
            date(2025, 10, 21): "Diwali Laxmi Pujan",
            date(2025, 10, 22): "Diwali Balipratipada",
            date(2025, 11,  5): "Guru Nanak Jayanti",
            date(2025, 12, 25): "Christmas",
            date(2026,  1, 26): "Republic Day",
            date(2026,  4, 14): "Ambedkar Jayanti",
            date(2026,  5,  1): "Maharashtra Day",
            date(2026,  8, 15): "Independence Day",
            date(2026, 10,  2): "Gandhi Jayanti",
            date(2026, 12, 25): "Christmas",
        }
        return _names.get(d)


# Singleton — import and use directly
calendar = MarketCalendar()
