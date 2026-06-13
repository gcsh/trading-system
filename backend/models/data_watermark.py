"""MITS Phase 11.G — per (source, ticker) sync watermark.

One row per (source, ticker) pair. The :mod:`backend.bot.data.sync_orchestrator`
reads this on delta-sync to figure out where to resume; writes the new
high-water-mark on every successful pull.

Why a separate table from :class:`IngestWatermark`: IngestWatermark
tracks live-outcome ingestion (Trade rows → MarketObservation). This
table tracks EXTERNAL-vendor pulls (ThetaData stock bars, IV history,
FRED series). Separating them keeps the semantics clean and makes the
"how far back have we synced?" question trivial.

External-cache-shaped — survives a paper reset (re-pulling the same
two-decade history on every reset would waste hours and produce zero
new value). Listed in :const:`EXTERNAL_CACHE_TABLES`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class DataWatermark(Base):
    __tablename__ = "data_watermarks"
    __table_args__ = (
        UniqueConstraint("source", "ticker", name="uq_data_watermark_source_ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Source key — e.g. "thetadata_stocks_daily", "thetadata_stocks_intraday_1m",
    # "thetadata_iv_history", "fred". Free-form so future sources can be added
    # without an enum migration.
    source: Mapped[str] = mapped_column(String, index=True)
    # Ticker / series_id. For FRED rows we store the series_id (e.g. "DGS10")
    # here so the same table covers both ticker-keyed and series-keyed sources.
    ticker: Mapped[str] = mapped_column(String, index=True)
    # UTC ISO timestamp of the last successful sync. NULL until first sync.
    last_synced_ts: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True)
    # Furthest-forward CALENDAR date we have a row for (string YYYY-MM-DD).
    # Persisted as ISO string for trivial SQL comparison + cross-platform.
    last_synced_through_date: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)
    # Row count from the last sync. Useful for monitoring.
    rows_last_sync: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[int] = mapped_column(Integer, default=1)
    error_text: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "ticker": self.ticker,
            "last_synced_ts": (self.last_synced_ts.isoformat()
                                if self.last_synced_ts else None),
            "last_synced_through_date": self.last_synced_through_date,
            "rows_last_sync": int(self.rows_last_sync or 0),
            "success": bool(self.success),
            "error_text": self.error_text,
            "updated_at": (self.updated_at.isoformat()
                            if self.updated_at else None),
        }
