"""MITS Phase 7.1 — IntradayRegimeEvent.

One row per intraday regime state transition. The engine's intraday
regime classifier reads SPY 30-min returns + VIX + curve + breadth +
put/call + sector dispersion every cycle and labels the tape with one
of ``normal | trending_up | trending_down | panic | capitulation |
squeeze | chop``. When the new state differs from the cached prior
state, we persist a row here so operators can audit the full sequence
of intraday regime transitions for a given session.

This is a derived/learned model — its rows come from public market
data, not bot decisions, so it survives ``fresh_start`` (added to
``EXTERNAL_CACHE_TABLES`` in ``backend/bot/system_reset.py``).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class IntradayRegimeEvent(Base):
    __tablename__ = "intraday_regime_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )
    prior_state: Mapped[str] = mapped_column(String, default="unknown", index=True)
    new_state: Mapped[str] = mapped_column(String, default="normal", index=True)
    severity: Mapped[str] = mapped_column(String, default="low")  # low | medium | high

    # The inputs that triggered the transition — frozen at the moment of the
    # transition so the autopsy can replay why the classifier flipped.
    spy_pct_change_30m: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, default=None
    )
    vix_spot: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, default=None
    )
    vix_curve_slope: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, default=None
    )
    breadth_ratio: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, default=None
    )
    put_call_ratio: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, default=None
    )
    sector_dispersion: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, default=None
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_at": self.event_at.isoformat() if self.event_at else None,
            "prior_state": self.prior_state,
            "new_state": self.new_state,
            "severity": self.severity,
            "spy_pct_change_30m": self.spy_pct_change_30m,
            "vix_spot": self.vix_spot,
            "vix_curve_slope": self.vix_curve_slope,
            "breadth_ratio": self.breadth_ratio,
            "put_call_ratio": self.put_call_ratio,
            "sector_dispersion": self.sector_dispersion,
        }
