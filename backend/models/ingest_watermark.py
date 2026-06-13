"""MITS Phase 6 (P6.1) — Ingest watermark for live-outcome → corpus ingestion.

The nightly `_ingest_live_outcomes` job walks closed Trade rows and
converts each into a MarketObservation + MarketOutcome pair so the
knowledge graph reflects live performance. To stay idempotent across
re-runs we record the last processed Trade id per ingest source.

Single-row-per-source semantics: there's one watermark per ingest
source name (default `"live_outcome_ingest"`). The nightly job reads
the row, processes Trade.id > last_ingested_trade_id, updates the row.

This is *external-cache-shaped* data: it gets preserved across
`fresh_start` because re-ingestion of historical trades would
double-count. It lives in `EXTERNAL_CACHE_TABLES`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class IngestWatermark(Base):
    __tablename__ = "ingest_watermarks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, unique=True, index=True)
    last_ingested_trade_id: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True)
    rows_ingested_total: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "last_ingested_trade_id": int(self.last_ingested_trade_id or 0),
            "last_run_at": (self.last_run_at.isoformat()
                                  if self.last_run_at else None),
            "rows_ingested_total": int(self.rows_ingested_total or 0),
        }
