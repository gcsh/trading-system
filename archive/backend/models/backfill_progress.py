"""MITS Phase 11.G — chunked-backfill progress ledger.

The orchestrator slices a long backfill window (e.g. 2006-01-01 →
2026-06-09 for daily bars) into chunks and persists progress per chunk.
On restart the orchestrator skips chunks already marked ``done`` and
picks the first ``in_progress`` / ``error`` chunk to resume.

PK: (source, ticker, date_range_start) — unique chunk identity. Multiple
chunks for the same (source, ticker) cover non-overlapping windows.

External-cache-shaped — preserved on paper reset because re-walking 20
years of history is the whole point of avoiding.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class BackfillProgress(Base):
    __tablename__ = "backfill_progress"
    __table_args__ = (
        UniqueConstraint(
            "source", "ticker", "date_range_start",
            name="uq_backfill_progress_source_ticker_start",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, index=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    # Chunk's inclusive [start, end] range. Strings for trivial SQL ordering.
    date_range_start: Mapped[str] = mapped_column(String, index=True)
    date_range_end: Mapped[str] = mapped_column(String)
    # ``pending`` → not started. ``in_progress`` → orchestrator picked it up
    # but didn't finish (crash or still running). ``done`` → finished
    # successfully. ``error`` → final attempt failed, see error_text.
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    # Last calendar date inside the chunk that we successfully processed.
    # Used by intra-chunk resume to pick up where the crash left off.
    last_completed_date: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error_text: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "ticker": self.ticker,
            "date_range_start": self.date_range_start,
            "date_range_end": self.date_range_end,
            "status": self.status,
            "last_completed_date": self.last_completed_date,
            "rows_written": int(self.rows_written or 0),
            "retry_count": int(self.retry_count or 0),
            "error_text": self.error_text,
            "started_at": (self.started_at.isoformat()
                            if self.started_at else None),
            "completed_at": (self.completed_at.isoformat()
                                if self.completed_at else None),
        }
