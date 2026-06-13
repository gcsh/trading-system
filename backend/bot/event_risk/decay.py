"""Stage-10 item 8 — event-risk decay tails.

Currently ``event_risk.can_trade()`` is binary: in-window = no, outside = yes.
Real desks reopen GRADUALLY after a macro print because:
  • The first few minutes after CPI / FOMC see volatility expansion +
    spread widening; full-size entries here pay punitive slippage
  • Losses cluster on "reaction trades" placed too quickly
  • Allowing 30–60 minutes of decay before full size gives the tape time
    to settle

Decay model (post-event only — pre-event is still a hard hold):
  +0 to +30 min   → multiplier 0.0  (full hold)
  +30 to +60 min  → multiplier 0.25 (quarter size)
  +60 to +120 min → multiplier 0.50 (half size)
  > +120 min      → multiplier 1.00 (normal)

Pre-event still uses the existing hard ±30 min hold. The decay only kicks
in AFTER the event timestamp passes.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class DecayResult:
    in_decay_window: bool
    minutes_since_event: Optional[float] = None
    size_multiplier: float = 1.0
    event_name: Optional[str] = None
    reason: str = ""
    suggested_resume_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_DECAY_BANDS: List[Dict[str, Any]] = [
    {"upper_minutes": 30.0, "multiplier": 0.0,  "label": "full hold"},
    {"upper_minutes": 60.0, "multiplier": 0.25, "label": "quarter size"},
    {"upper_minutes": 120.0, "multiplier": 0.50, "label": "half size"},
]
_FINAL_MULTIPLIER = 1.0


def _band_for(minutes_since: float) -> Dict[str, Any]:
    for band in _DECAY_BANDS:
        if minutes_since < band["upper_minutes"]:
            return band
    return {"upper_minutes": float("inf"),
             "multiplier": _FINAL_MULTIPLIER,
             "label": "normal"}


def _parse_event_time(when: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(when)
    except Exception:
        return None


def decay_multiplier(*, now: Optional[datetime] = None,
                       lookback_hours: int = 4) -> DecayResult:
    """Walk the most-recent high-impact events. Return the size multiplier
    that applies right now, plus a suggested_resume_at timestamp."""
    from backend.bot.event_risk import _macro_events_for_year

    now = now or datetime.utcnow()
    horizon = now - timedelta(hours=lookback_hours)
    most_recent: Optional[Any] = None      # nearest event in the lookback
    for year in {now.year, horizon.year}:
        for ev in _macro_events_for_year(year):
            if ev.impact != "high":
                continue
            t = _parse_event_time(ev.when)
            if t is None or t > now or t < horizon:
                continue
            if most_recent is None or t > _parse_event_time(most_recent.when):
                most_recent = ev

    if most_recent is None:
        return DecayResult(in_decay_window=False, size_multiplier=1.0,
                              reason="no high-impact event in lookback window")

    minutes_since = (now - _parse_event_time(most_recent.when)).total_seconds() / 60.0
    band = _band_for(minutes_since)
    in_decay = band["multiplier"] < 1.0
    resume_at = (_parse_event_time(most_recent.when)
                   + timedelta(minutes=_DECAY_BANDS[-1]["upper_minutes"])).isoformat()
    return DecayResult(
        in_decay_window=in_decay,
        minutes_since_event=round(minutes_since, 1),
        size_multiplier=band["multiplier"],
        event_name=most_recent.name,
        reason=(f"{minutes_since:.0f} min since '{most_recent.name}' → "
                  f"{band['label']} (× {band['multiplier']:.2f})"),
        suggested_resume_at=resume_at,
    )


def can_trade_with_decay(*, ticker: str, now: Optional[datetime] = None
                            ) -> Dict[str, Any]:
    """Combined check — hard hold via `can_trade()` first, then decay
    multiplier afterward. Returns a dict the engine can plug straight in.

    Stage-18b: extended to consult SEC EDGAR's recent material filings.
    An 8-K with a material item code (results announcement, officer
    change, material agreement) inside the last 48h widens the no-go
    window so we don't trade through a known-material event.
    """
    from backend.bot.event_risk import can_trade

    perm = can_trade(ticker, now=now)
    if not perm.can_trade:
        # Pre-event / earnings hold — decay doesn't apply, binary block.
        return {
            "ticker": ticker.upper(),
            "can_trade": False, "size_multiplier": 0.0,
            "reason": perm.reason, "decay": None,
            "next_window": perm.next_window,
        }

    # Stage-18b — SEC material-event veto. Tolerant of missing user agent
    # / cold-start cache; returns False when no flagged filing exists.
    try:
        from backend.bot.data.edgar import has_material_event
        if has_material_event(ticker, within_hours=24):
            return {
                "ticker": ticker.upper(),
                "can_trade": False, "size_multiplier": 0.0,
                "reason": ("material SEC filing in the last 24h "
                              "(8-K / 10-Q / 10-K / S-3) — pause"),
                "decay": None, "next_window": None,
                "edgar_material": True,
            }
    except Exception:
        pass

    decay = decay_multiplier(now=now)
    return {
        "ticker": ticker.upper(),
        "can_trade": decay.size_multiplier > 0,
        "size_multiplier": decay.size_multiplier,
        "reason": decay.reason if decay.in_decay_window else "no decay constraint",
        "decay": decay.to_dict() if decay.in_decay_window else None,
        "next_window": decay.suggested_resume_at if decay.in_decay_window else None,
    }
