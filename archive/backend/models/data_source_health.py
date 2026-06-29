"""MITS Phase 11.I — per-source health snapshot row.

One row per (source, date). Aggregates the day's pull attempts +
successes + rows written + average latency so the Lake Status UI can
render a 9-source health grid.

External-cache-shaped — survives a paper reset (derived from public
vendor calls + the backfill_progress ledger, not bot decisions).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Date, DateTime, Float, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class DataSourceHealth(Base):
    __tablename__ = "data_source_health"
    __table_args__ = (
        UniqueConstraint(
            "source", "snapshot_date",
            name="uq_data_source_health_source_date",
        ),
        Index("ix_data_source_health_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    pulls_attempted: Mapped[int] = mapped_column(Integer, default=0)
    pulls_successful: Mapped[int] = mapped_column(Integer, default=0)
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    avg_latency_ms: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)
    last_error_text: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)
    # "green" | "yellow" | "red"
    #   green  = 100% success + rows >= threshold
    #   yellow = 80-99% success
    #   red    = <80% success or no data in last 24h
    status: Mapped[str] = mapped_column(String, default="green")
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "snapshot_date": (self.snapshot_date.isoformat()
                                if self.snapshot_date else None),
            "pulls_attempted": int(self.pulls_attempted or 0),
            "pulls_successful": int(self.pulls_successful or 0),
            "rows_written": int(self.rows_written or 0),
            "avg_latency_ms": self.avg_latency_ms,
            "last_error_text": self.last_error_text,
            "status": self.status,
            "computed_at": (self.computed_at.isoformat()
                            if self.computed_at else None),
        }
