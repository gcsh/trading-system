"""US market calendar — single source of truth for "is NYSE open right now?".

Lightweight: NYSE regular hours (9:30 AM - 4:00 PM ET, Mon-Fri) plus a
static list of US market holidays. No external dependencies. Pull in
pandas-market-calendars later if we ever need pre/post-market or
intraday-close coverage.

Used by the engine's run_cycle gate to skip cycles when the market is
closed — saves ~70% of daily AI Brain spend (market is closed
overnight + weekends + holidays = 67%+ of clock time, and during
those hours yfinance prices are stale, no trades can execute, and
brain reasoning is wasted tokens).
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Optional

try:
    from zoneinfo import ZoneInfo  # py3.9+
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — fallback for stripped-down envs
    _ET = None

# NYSE market holidays. Extend yearly. (Early-close days are treated as
# full close for cost-saving purposes — losing 3 hrs of a half-day is
# acceptable; the alternative is full pandas-market-calendars.)
_HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
    "2027-11-25", "2027-12-24",
}

_OPEN = time(9, 30)
_CLOSE = time(16, 0)


def is_us_market_open(now: Optional[datetime] = None) -> bool:
    """True if NYSE is currently in regular trading hours."""
    if _ET is None:
        return True  # fail-open if zoneinfo is unavailable
    if now is None:
        now = datetime.now(tz=_ET)
    else:
        now = now.astimezone(_ET) if now.tzinfo else now.replace(tzinfo=_ET)
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in _HOLIDAYS:
        return False
    t = now.timetz().replace(tzinfo=None)
    return _OPEN <= t < _CLOSE


def minutes_until_close(now: Optional[datetime] = None) -> Optional[float]:
    """Return minutes remaining until the 16:00 ET regular-hours close,
    or ``None`` when NYSE is not currently open (weekend / holiday /
    pre-market / after-hours).

    Used by the engine's must_exit_by_eod sweep: positions tagged
    ``must_exit_by_eod=True`` get force-closed when minutes_until_close
    drops below ``TUNABLES.eod_close_minutes_before_close``.
    """
    if _ET is None:
        return None
    if now is None:
        now = datetime.now(tz=_ET)
    else:
        now = now.astimezone(_ET) if now.tzinfo else now.replace(tzinfo=_ET)
    if now.weekday() >= 5:
        return None
    if now.strftime("%Y-%m-%d") in _HOLIDAYS:
        return None
    t = now.timetz().replace(tzinfo=None)
    if not (_OPEN <= t < _CLOSE):
        return None
    close_dt = now.replace(hour=_CLOSE.hour, minute=_CLOSE.minute,
                              second=0, microsecond=0)
    return max(0.0, (close_dt - now).total_seconds() / 60.0)


def market_status(now: Optional[datetime] = None) -> dict:
    """Structured status — useful for UI/logging.

    Returns: {open, reason, et_now}
    """
    if _ET is None:
        return {"open": True, "reason": "no-tz (fail-open)", "et_now": None}
    if now is None:
        now = datetime.now(tz=_ET)
    et_now = now.astimezone(_ET) if now.tzinfo else now.replace(tzinfo=_ET)
    et_iso = et_now.isoformat()
    if et_now.weekday() >= 5:
        return {"open": False, "reason": "weekend", "et_now": et_iso}
    if et_now.strftime("%Y-%m-%d") in _HOLIDAYS:
        return {"open": False, "reason": "holiday", "et_now": et_iso}
    t = et_now.timetz().replace(tzinfo=None)
    if t < _OPEN:
        return {"open": False, "reason": "pre-market", "et_now": et_iso}
    if t >= _CLOSE:
        return {"open": False, "reason": "after-hours", "et_now": et_iso}
    return {"open": True, "reason": "regular-hours", "et_now": et_iso}
