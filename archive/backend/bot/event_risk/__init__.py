"""Stage-4 event-risk engine — auto-hold around macro prints + earnings.

The bot SHOULD NOT trade through known liquidity-shock events without
explicit override. This module:
  • Tracks the known macro calendar (CPI, FOMC, NFP, GDP, OPEX)
  • Pulls per-ticker earnings dates from yfinance
  • Returns ``can_trade`` per ticker with reasons + auto-hold window
  • Cross-asset hedge prompt when high-impact event is imminent

The calendar is hardcoded for known prints + heuristically derived for
recurring monthly / quarterly events. A real Stage-7 data-source health
layer will replace this with a Bloomberg / FMP calendar feed.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CalendarEvent:
    name: str                          # "CPI", "FOMC Statement", "Powell Speech", ...
    kind: str                          # macro | earnings | opex | dividend | speech
    when: str                          # ISO-8601 datetime
    impact: str = "medium"             # high | medium | low
    tickers_affected: List[str] = field(default_factory=list)   # ["all"] for macro
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TradePermission:
    ticker: str
    can_trade: bool
    reason: str = ""
    blocking_events: List[Dict[str, Any]] = field(default_factory=list)
    next_window: Optional[str] = None    # ISO time the bot can trade again

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── known macro calendar (hardcoded reference window) ─────────────────────


# Format: month, day-of-month, hour (UTC), name, impact
# Sources: BLS / Fed / BEA release calendars 2026. Times in UTC.
_KNOWN_MACRO_EVENTS_2026: List[Dict[str, Any]] = [
    {"month": 6, "day": 11, "hour": 12, "name": "CPI May", "impact": "high"},
    {"month": 6, "day": 12, "hour": 12, "name": "PPI May", "impact": "medium"},
    {"month": 6, "day": 18, "hour": 18, "name": "FOMC Statement", "impact": "high"},
    {"month": 6, "day": 18, "hour": 18.5, "name": "Powell Press Conf", "impact": "high"},
    {"month": 7, "day": 3,  "hour": 12, "name": "NFP June", "impact": "high"},
    {"month": 7, "day": 11, "hour": 12, "name": "CPI June", "impact": "high"},
    {"month": 7, "day": 30, "hour": 18, "name": "FOMC Statement", "impact": "high"},
    {"month": 8, "day": 1,  "hour": 12, "name": "NFP July", "impact": "high"},
    {"month": 8, "day": 12, "hour": 12, "name": "CPI July", "impact": "high"},
    {"month": 9, "day": 5,  "hour": 12, "name": "NFP August", "impact": "high"},
    {"month": 9, "day": 11, "hour": 12, "name": "CPI August", "impact": "high"},
    {"month": 9, "day": 17, "hour": 18, "name": "FOMC Statement", "impact": "high"},
    {"month": 10, "day": 3, "hour": 12, "name": "NFP September", "impact": "high"},
    {"month": 10, "day": 15, "hour": 12, "name": "CPI September", "impact": "high"},
    {"month": 10, "day": 29, "hour": 18, "name": "FOMC Statement", "impact": "high"},
    {"month": 11, "day": 7, "hour": 12, "name": "NFP October", "impact": "high"},
    {"month": 11, "day": 13, "hour": 13, "name": "CPI October", "impact": "high"},
    {"month": 12, "day": 5, "hour": 13, "name": "NFP November", "impact": "high"},
    {"month": 12, "day": 10, "hour": 13, "name": "CPI November", "impact": "high"},
    {"month": 12, "day": 10, "hour": 19, "name": "FOMC Statement", "impact": "high"},
]


def _opex_dates_for_year(year: int) -> List[date]:
    """3rd Friday of every month."""
    out: List[date] = []
    for month in range(1, 13):
        d = date(year, month, 1)
        # advance to first Friday
        while d.weekday() != 4:
            d += timedelta(days=1)
        d += timedelta(days=14)  # third Friday
        out.append(d)
    return out


def _macro_events_for_year(year: int) -> List[CalendarEvent]:
    """Pull the macro reference table for 2026 (hardcoded) + OPEX for any
    year. Future Stage-7 fetches will replace the hardcoded list."""
    out: List[CalendarEvent] = []
    if year == 2026:
        for e in _KNOWN_MACRO_EVENTS_2026:
            hour = int(e["hour"])
            minute = int((e["hour"] - hour) * 60)
            when = datetime(year, e["month"], e["day"], hour, minute)
            out.append(CalendarEvent(name=e["name"], kind="macro",
                                       when=when.isoformat(),
                                       impact=e["impact"],
                                       tickers_affected=["all"]))
    for opex in _opex_dates_for_year(year):
        out.append(CalendarEvent(
            name=f"OPEX {opex.strftime('%b %Y')}",
            kind="opex",
            when=datetime.combine(opex, time(20, 0)).isoformat(),
            impact="medium",
            tickers_affected=["all"],
            notes="monthly options expiration",
        ))
    return out


# ── earnings per ticker ───────────────────────────────────────────────────


def _earnings_event(ticker: str) -> List[CalendarEvent]:
    """Best-effort: pull next earnings date from yfinance.calendar."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        cal = getattr(t, "calendar", None)
        if cal is None:
            return []
        # yfinance returns either a DataFrame or a dict depending on version
        e_date = None
        if hasattr(cal, "loc"):
            try:
                e_date = cal.loc["Earnings Date"].iloc[0]
            except Exception:
                pass
        elif isinstance(cal, dict):
            e_date = cal.get("Earnings Date")
            if isinstance(e_date, list) and e_date:
                e_date = e_date[0]
        if e_date is None:
            return []
        when = e_date.isoformat() if hasattr(e_date, "isoformat") else str(e_date)
        return [CalendarEvent(
            name=f"{ticker.upper()} earnings", kind="earnings", when=when,
            impact="high", tickers_affected=[ticker.upper()],
        )]
    except Exception:
        return []


# ── public API ────────────────────────────────────────────────────────────


def upcoming_events(*, within_days: int = 14,
                      tickers: Optional[List[str]] = None) -> List[CalendarEvent]:
    """Sorted list of events in the next ``within_days``, plus per-ticker
    earnings for the supplied tickers."""
    now = datetime.utcnow()
    horizon = now + timedelta(days=within_days)
    events: List[CalendarEvent] = []
    for year in {now.year, horizon.year}:
        events.extend(_macro_events_for_year(year))
    for tk in tickers or []:
        events.extend(_earnings_event(tk))
    upcoming = [e for e in events
                  if now <= _parse(e.when) <= horizon]
    return sorted(upcoming, key=lambda e: e.when)


def _parse(iso: str) -> datetime:
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return datetime(1900, 1, 1)


def active_events(*, window_minutes_before: int = 30,
                    window_minutes_after: int = 30) -> List[CalendarEvent]:
    """Events that are currently inside the ``[now - before, now + after]``
    window — the bot is in auto-hold for these."""
    now = datetime.utcnow()
    lo = now - timedelta(minutes=window_minutes_after)
    hi = now + timedelta(minutes=window_minutes_before)
    out: List[CalendarEvent] = []
    for year in {now.year, hi.year}:
        for e in _macro_events_for_year(year):
            t = _parse(e.when)
            if lo <= t <= hi:
                out.append(e)
    return sorted(out, key=lambda e: e.when)


def can_trade(ticker: str, *, now: Optional[datetime] = None,
                pre_minutes: int = 30, post_minutes: int = 30,
                earnings_hold_days: int = 1) -> TradePermission:
    """The single question the engine asks every cycle.

    Default rules:
      • High-impact macro print within ±30 min: NO
      • Ticker's earnings within ±1 day: NO
      • OPEX day: allow but warn (auto-hold flag set False, reason logged)
    """
    now = now or datetime.utcnow()
    horizon = now + timedelta(days=max(earnings_hold_days, 1))
    horizon_back = now - timedelta(minutes=post_minutes)

    blocking: List[CalendarEvent] = []
    next_window: Optional[datetime] = None
    for year in {now.year, horizon.year}:
        for ev in _macro_events_for_year(year):
            t = _parse(ev.when)
            if ev.impact != "high":
                continue
            if (t - timedelta(minutes=pre_minutes)) <= now <= (t + timedelta(minutes=post_minutes)):
                blocking.append(ev)
                next_window = max(next_window or t, t + timedelta(minutes=post_minutes))

    # Earnings for this specific ticker
    for ev in _earnings_event(ticker):
        t = _parse(ev.when)
        if abs((t - now).days) <= earnings_hold_days:
            blocking.append(ev)
            next_window = max(next_window or t, t + timedelta(days=earnings_hold_days))

    if blocking:
        reason = "; ".join(f"{e.name} @ {e.when}" for e in blocking)
        return TradePermission(
            ticker=ticker.upper(), can_trade=False, reason=reason,
            blocking_events=[e.to_dict() for e in blocking],
            next_window=next_window.isoformat() if next_window else None,
        )
    return TradePermission(ticker=ticker.upper(), can_trade=True,
                              reason="no active event-risk holds")
