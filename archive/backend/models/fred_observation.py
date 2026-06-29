"""Stage-18a — FRED economic series observations.

One row per (series_id, date). Daily-grain series get one row per day;
weekly/monthly series get one row per release. Fetched on a daily cron
into this cache so downstream consumers (macro agent, regime classifier)
read locally and don't hammer the FRED API.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class FredObservation(Base):
    __tablename__ = "fred_observations"
    __table_args__ = (UniqueConstraint("series_id", "date",
                                            name="uq_fred_series_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series_id: Mapped[str] = mapped_column(String, index=True)
    date: Mapped[datetime] = mapped_column(DateTime, index=True)
    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "series_id": self.series_id,
            "date": self.date.isoformat() if self.date else None,
            "value": self.value,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }
