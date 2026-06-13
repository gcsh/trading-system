"""Stage-18a — Market breadth daily snapshot.

One row per trading day. The breadth engine fetches the S&P 500
constituent universe and computes per-day breadth stats (% above each
MA, A/D, new highs/lows, McClellan). This is the *substrate* the
regime classifier reads to know whether SPY +1% is healthy or fragile.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class BreadthSnapshot(Base):
    __tablename__ = "breadth_snapshots"
    __table_args__ = (UniqueConstraint("date", "universe",
                                            name="uq_breadth_date_universe"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[datetime] = mapped_column(DateTime, index=True)
    universe: Mapped[str] = mapped_column(String, default="sp500")

    pct_above_20dma: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pct_above_50dma: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pct_above_200dma: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    advancers: Mapped[int] = mapped_column(Integer, default=0)
    decliners: Mapped[int] = mapped_column(Integer, default=0)
    new_highs: Mapped[int] = mapped_column(Integer, default=0)
    new_lows: Mapped[int] = mapped_column(Integer, default=0)
    ad_line: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mcclellan: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat() if self.date else None,
            "universe": self.universe,
            "pct_above_20dma": self.pct_above_20dma,
            "pct_above_50dma": self.pct_above_50dma,
            "pct_above_200dma": self.pct_above_200dma,
            "advancers": self.advancers, "decliners": self.decliners,
            "new_highs": self.new_highs, "new_lows": self.new_lows,
            "ad_line": self.ad_line, "mcclellan": self.mcclellan,
            "sample_size": self.sample_size,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }
