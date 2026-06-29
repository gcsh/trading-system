"""Stage-P1.3 — per-ticker per-date implied-volatility history.

One row per (ticker, date). Populated two ways:

  1. Incrementally on every ``options_snapshot`` call — today's IV gets
     written here so the rank corpus grows organically as the bot trades.
  2. By an explicit backfill (``backend.bot.data.iv_history.backfill``)
     that walks ThetaData's historical EOD endpoint to populate a year
     (or eight years, on Standard) of history per ticker.

External-cache pattern (same shape as FredObservation): never wiped on
``fresh_start`` because the data is sourced from outside the bot's own
decisions. Listed in ``EXTERNAL_CACHE_TABLES`` in ``system_reset.py``.

Why a separate table instead of computing on-demand:
  - True IV rank requires 1+ year of history. We can't fetch a year of
    EOD straddles on every ``options_snapshot`` call — would be ~500
    ThetaData hits per cycle per ticker.
  - The percentile query is a single SQL aggregate over ~250 rows.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class IVHistory(Base):
    __tablename__ = "iv_history"
    __table_args__ = (UniqueConstraint("ticker", "date",
                                            name="uq_iv_history_ticker_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    date: Mapped[datetime] = mapped_column(DateTime, index=True)
    iv_atm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # The expiry whose ATM straddle gave us this IV — needed when the
    # backfill picks different expiries across dates (always closest to
    # 30 DTE) so we can audit and re-derive if formulas change.
    expiry_used: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    dte_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Source tag so we can later distinguish backfilled values from
    # live-captured ones if the methodology diverges.
    source: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "date": self.date.isoformat() if self.date else None,
            "iv_atm": self.iv_atm,
            "expiry_used": self.expiry_used.isoformat() if self.expiry_used else None,
            "dte_used": self.dte_used,
            "source": self.source,
        }
