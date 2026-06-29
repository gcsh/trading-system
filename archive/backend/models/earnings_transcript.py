"""MITS Phase 11.D — AlphaVantage earnings call transcript header row.

One row per (ticker, fiscal_year, fiscal_quarter). The raw concatenated
full text is stored in :attr:`full_text` for downstream regex / search.
Per-speaker-turn paragraphs are written to
:class:`backend.models.transcript_paragraph.TranscriptParagraph` so the
vector embedding pipeline (Agent 4) can index at paragraph grain
without rebuilding the source text every call.

External-cache-shaped — survives a paper reset. The AlphaVantage free
tier is 25 req/day so the full 5y × 40-ticker backfill (~800 calls)
takes ~32 days. Wiping it on reset would force another month-long
rebuild for zero value.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Date, DateTime, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class EarningsTranscript(Base):
    __tablename__ = "earnings_transcripts"
    __table_args__ = (
        UniqueConstraint("ticker", "fiscal_year", "fiscal_quarter",
                         name="uq_earnings_transcripts_ticker_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    fiscal_year: Mapped[int] = mapped_column(Integer, index=True)
    # 1-4. Stored as an int so range queries work.
    fiscal_quarter: Mapped[int] = mapped_column(Integer, index=True)
    # Report date if AlphaVantage exposes one; else NULL.
    report_date: Mapped[Optional[date]] = mapped_column(
        Date, nullable=True, index=True)
    # Concatenated full text (speaker turns joined with newlines).
    # Useful for grep / regex feature surfaces that don't want to JOIN
    # the paragraph table. Capped at ~2MB by the writer to keep SQLite
    # row sizes reasonable.
    full_text: Mapped[str] = mapped_column(Text, default="")
    # JSON-serialized AlphaVantage metadata blob (symbol, quarter,
    # speaker count, etc) — preserved for replay / audit.
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    paragraph_count: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "fiscal_year": self.fiscal_year,
            "fiscal_quarter": self.fiscal_quarter,
            "report_date": (self.report_date.isoformat()
                             if self.report_date else None),
            "paragraph_count": int(self.paragraph_count or 0),
            "full_text_len": len(self.full_text or ""),
            "metadata_json": self.metadata_json,
            "fetched_at": (self.fetched_at.isoformat()
                            if self.fetched_at else None),
        }
