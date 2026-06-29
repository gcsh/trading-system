"""Stage-18b — CFTC Commitment of Traders cache.

One row per (instrument, report_date). The macro agent reads positioning
extremes on E-mini S&P, Treasury futures, and DXY futures.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class CotReport(Base):
    __tablename__ = "cot_reports"
    __table_args__ = (UniqueConstraint("instrument", "report_date",
                                            name="uq_cot_instrument_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument: Mapped[str] = mapped_column(String, index=True)
    report_date: Mapped[datetime] = mapped_column(DateTime, index=True)

    # Disaggregated commitments
    noncommercial_long: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    noncommercial_short: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    commercial_long: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    commercial_short: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    open_interest: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        noncomm_net = None
        if (self.noncommercial_long is not None
                and self.noncommercial_short is not None):
            noncomm_net = self.noncommercial_long - self.noncommercial_short
        comm_net = None
        if (self.commercial_long is not None
                and self.commercial_short is not None):
            comm_net = self.commercial_long - self.commercial_short
        return {
            "instrument": self.instrument,
            "report_date": (self.report_date.isoformat()
                              if self.report_date else None),
            "noncommercial_long": self.noncommercial_long,
            "noncommercial_short": self.noncommercial_short,
            "commercial_long": self.commercial_long,
            "commercial_short": self.commercial_short,
            "open_interest": self.open_interest,
            "noncommercial_net": noncomm_net,
            "commercial_net": comm_net,
        }
