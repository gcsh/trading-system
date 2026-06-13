"""Stage-18b — FINRA short interest cache.

FINRA publishes settlement-date short interest twice a month. One row
per (ticker, settlement_date). The microstructure agent reads the
latest row to flag potential squeeze candidates (rising short interest
× breakout). Free public data; no API key needed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class ShortInterest(Base):
    __tablename__ = "short_interest"
    __table_args__ = (UniqueConstraint("ticker", "settlement_date",
                                            name="uq_si_ticker_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    settlement_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    short_interest: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_daily_volume: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    days_to_cover: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "settlement_date": (self.settlement_date.isoformat()
                                  if self.settlement_date else None),
            "short_interest": self.short_interest,
            "avg_daily_volume": self.avg_daily_volume,
            "days_to_cover": self.days_to_cover,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }
