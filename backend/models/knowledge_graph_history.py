"""MITS Phase 1 — Knowledge Graph History (sparkline source).

Daily snapshot of `knowledge_graph` cells so the drill-down UI can
render a real time-series sparkline of posterior win rate, sample size,
and confidence bounds.

Indexed on (ticker, pattern, snapshot_date) for the per-cell history
lookup that the API endpoint performs.

Unique on the full 7-axis cohort key + snapshot_date so the nightly
snapshot job is idempotent (re-running on the same calendar day
overwrites the row instead of duplicating).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Date, DateTime, Float, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class KnowledgeGraphHistory(Base):
    __tablename__ = "knowledge_graph_history"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "pattern", "regime", "vol_state", "time_bucket",
            "horizon", "sample_split", "snapshot_date",
            name="uq_kgh_cohort_date",
        ),
        Index("ix_kgh_ticker_pattern", "ticker", "pattern"),
        Index("ix_kgh_snapshot_date", "snapshot_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    pattern: Mapped[str] = mapped_column(String, index=True)
    regime: Mapped[str] = mapped_column(String, default="unknown")
    vol_state: Mapped[str] = mapped_column(String, default="normal")
    time_bucket: Mapped[str] = mapped_column(String, default="rth")
    horizon: Mapped[str] = mapped_column(String, default="1d")
    sample_split: Mapped[str] = mapped_column(String, default="combined")

    # Date of the snapshot — one row per cell per calendar day.
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)

    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    posterior_win_rate: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
    )
    avg_return_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence_lower: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence_upper: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "pattern": self.pattern,
            "regime": self.regime,
            "vol_state": self.vol_state,
            "time_bucket": self.time_bucket,
            "horizon": self.horizon,
            "sample_split": self.sample_split,
            "snapshot_date": (self.snapshot_date.isoformat()
                                       if self.snapshot_date else None),
            "sample_size": self.sample_size,
            "win_rate": self.win_rate,
            "posterior_win_rate": self.posterior_win_rate,
            "avg_return_pct": self.avg_return_pct,
            "confidence_lower": self.confidence_lower,
            "confidence_upper": self.confidence_upper,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
