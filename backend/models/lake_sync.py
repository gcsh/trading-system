"""MITS Phase 8 — S3 lake sync metadata.

One row per (layer, table_or_source) tracks the watermark of the most
recent successful sync to the lake. Lets the silver/gold/vector cron
jobs resume mid-stream after a service restart without double-writing.

Per the fresh-start contract (memory/MEMORY.md), this table belongs to
``EXTERNAL_CACHE_TABLES`` — the sync log survives a paper-reset
because the underlying S3 + pgvector store also survives. Wiping the
local sync state would just cause an unnecessary re-scan.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from backend.db import Base


class LakeSyncWatermark(Base):
    __tablename__ = "lake_sync_watermark"

    id = Column(Integer, primary_key=True)
    layer = Column(String(32), index=True, nullable=False)        # bronze | silver | gold | vector
    scope = Column(String(128), index=True, nullable=False)        # "yfinance/bars" | "BarRow" | "trades" | "regime_snapshots"
    last_sync_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_row_id = Column(Integer, default=0, nullable=False)
    rows_written = Column(Integer, default=0, nullable=False)
    s3_uri = Column(String(512), default="", nullable=False)
    status = Column(String(32), default="ok", nullable=False)      # ok | partial | error
    detail = Column(String(512), default="", nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<LakeSyncWatermark layer={self.layer} scope={self.scope} "
                f"last_sync_at={self.last_sync_at} status={self.status}>")
