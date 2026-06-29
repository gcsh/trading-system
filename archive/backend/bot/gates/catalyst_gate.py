"""MITS Phase 5 (P5.5) — catalyst gate.

A trade-level gate that consults the earnings + FOMC calendars BEFORE
sizing. Outcomes:

  * passes=True  + conviction_multiplier == 1.0 → clean entry.
  * passes=True  + conviction_multiplier  < 1.0 → enter but size down
       (the conviction multiplier is fed into the sizing layer).
  * passes=False                                → abstain. Decision-log
       carries the gate reason so /prediction-outcomes can later cite
       "catalyst_gate: TSLA earnings 2026-06-09" in skip_reason.

All thresholds + multipliers come from ``TUNABLES`` so no magic numbers
live in this module. Earnings dates come from the existing event-risk
helper (yfinance.Ticker.calendar). FOMC dates come from the
event_risk macro table.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta
from typing import Any, Dict, List, Optional

from backend.config import TUNABLES

logger = logging.getLogger(__name__)


@dataclass
class CatalystGateResult:
    passes: bool = True
    conviction_multiplier: float = 1.0
    reason: Optional[str] = None
    triggers: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passes": self.passes,
            "conviction_multiplier": round(float(self.conviction_multiplier), 4),
            "reason": self.reason,
            "triggers": list(self.triggers),
        }


def _next_earnings_date(ticker: str, now: datetime) -> Optional[_date]:
    """Best-effort lookup of the next earnings date for ``ticker``.

    Uses the existing event_risk._earnings_event helper, which already
    handles yfinance's DataFrame/dict shape variance. Returns None when
    no upcoming earnings date is known (data unavailable, distant future).
    Failures are NEVER fatal — the gate fails open.
    """
    try:
        from backend.bot.event_risk import _earnings_event
        events = _earnings_event(ticker) or []
        for ev in events:
            when = ev.when
            try:
                # event_risk stores ISO strings; tolerate both with and
                # without time component.
                if "T" in when:
                    dt = datetime.fromisoformat(when.split("+")[0])
                    d = dt.date()
                else:
                    d = _date.fromisoformat(when[:10])
            except Exception:
                continue
            if d >= now.date():
                return d
    except Exception:
        logger.debug("catalyst_gate: earnings lookup failed for %s",
                       ticker, exc_info=True)
    return None


def _trading_days_between(start: _date, end: _date) -> int:
    """Approximate trading-day count (mon-fri, no holidays).

    Cheap inclusive count over a small window — accurate enough for
    the 5-day catalyst proximity check. We deliberately do NOT pull
    in pandas_market_calendars for one feature.
    """
    if end < start:
        start, end = end, start
    n = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    # Inclusive count includes both endpoints; subtract 1 so "today =
    # earnings day" returns 0 (already happening) — matches the
    # "earnings within N days" intuition.
    return max(0, n - 1)


def _next_fomc_meeting(now: datetime) -> Optional[datetime]:
    """Return the next FOMC statement datetime ≥ now, or None."""
    try:
        from backend.bot.event_risk import _macro_events_for_year
        candidates: List[datetime] = []
        for year in (now.year, now.year + 1):
            for ev in _macro_events_for_year(year):
                if ev.kind != "macro":
                    continue
                if "FOMC" not in (ev.name or ""):
                    continue
                try:
                    dt = datetime.fromisoformat(ev.when)
                except Exception:
                    continue
                if dt >= now:
                    candidates.append(dt)
        if not candidates:
            return None
        return min(candidates)
    except Exception:
        logger.debug("catalyst_gate: FOMC lookup failed", exc_info=True)
        return None


def check(ticker: str,
            instrument: Optional[str] = None,
            dte: Optional[int] = None,
            *,
            now: Optional[datetime] = None,
            short_dte_threshold: Optional[int] = None,
            ) -> CatalystGateResult:
    """Run the catalyst gate. Pure function — safe to call from tests.

    ``instrument`` is 'option' / 'spread' / 'stock'. The short-DTE
    earnings abstain only applies to options-style instruments;
    stocks fall through to the conviction-multiplier paths.

    ``dte`` is the option's days-to-expiry (None for stock).

    18-FU Gap R1 — ``short_dte_threshold`` overrides
    ``TUNABLES.catalyst_short_dte_threshold`` when the caller has
    resolved an operator-approved value via
    ``backend.bot.learning.policy_apply.resolve_threshold``. ``None``
    keeps the legacy TUNABLES lookup.
    """
    now = now or datetime.utcnow()
    instrument = (instrument or "stock").lower()
    triggers: List[Dict[str, Any]] = []

    earnings_window = int(getattr(TUNABLES, "catalyst_earnings_window_days", 5))
    earnings_mult = float(getattr(TUNABLES, "catalyst_earnings_multiplier", 0.5))
    fomc_window_hours = int(getattr(TUNABLES, "catalyst_fomc_window_hours", 24))
    fomc_mult = float(getattr(TUNABLES, "catalyst_fomc_multiplier", 0.5))
    if short_dte_threshold is None:
        short_dte_threshold = int(
            getattr(TUNABLES, "catalyst_short_dte_threshold", 7)
        )
    else:
        short_dte_threshold = int(short_dte_threshold)

    # ---- Earnings proximity --------------------------------------------
    earnings_date = _next_earnings_date(ticker, now)
    earnings_close = False
    if earnings_date is not None:
        td = _trading_days_between(now.date(), earnings_date)
        if td <= earnings_window:
            earnings_close = True
            triggers.append({
                "kind": "earnings",
                "date": earnings_date.isoformat(),
                "trading_days_away": td,
            })

    # ---- Short-DTE option into earnings: ABSTAIN -----------------------
    if (earnings_close and instrument in ("option", "spread")
            and dte is not None and dte <= short_dte_threshold):
        reason = (
            f"catalyst_gate: {ticker} earnings {earnings_date.isoformat()} "
            f"(~{triggers[0]['trading_days_away']}td) — option DTE={dte} "
            f"≤ {short_dte_threshold} threshold. ABSTAIN."
        )
        return CatalystGateResult(
            passes=False, conviction_multiplier=0.0,
            reason=reason, triggers=triggers,
        )

    # ---- FOMC proximity ------------------------------------------------
    fomc_dt = _next_fomc_meeting(now)
    fomc_close = False
    if fomc_dt is not None:
        delta_hours = (fomc_dt - now).total_seconds() / 3600.0
        if 0 <= delta_hours <= fomc_window_hours:
            fomc_close = True
            triggers.append({
                "kind": "fomc",
                "datetime": fomc_dt.isoformat(),
                "hours_away": round(delta_hours, 2),
            })

    # ---- Compose multiplier (earnings × FOMC compound) ----------------
    multiplier = 1.0
    parts: List[str] = []
    if earnings_close:
        multiplier *= earnings_mult
        parts.append(
            f"earnings ≤{earnings_window}td (×{earnings_mult:.2f})"
        )
    if fomc_close:
        multiplier *= fomc_mult
        parts.append(
            f"FOMC ≤{fomc_window_hours}h (×{fomc_mult:.2f})"
        )

    if multiplier == 1.0:
        return CatalystGateResult(
            passes=True, conviction_multiplier=1.0, reason=None,
            triggers=triggers,
        )

    return CatalystGateResult(
        passes=True,
        conviction_multiplier=multiplier,
        reason=f"catalyst_gate: {ticker} {' + '.join(parts)} → ×{multiplier:.2f}",
        triggers=triggers,
    )


__all__ = ["CatalystGateResult", "check"]
