"""MITS Phase 11.D — per-speaker-turn paragraph of an earnings transcript.

Each row is one speaker turn from one earnings call. Agent 4's vector
embedding pipeline indexes at this grain: a 5-minute Q&A exchange is
one row, an entire prepared-remarks dump is split into one row per
speaker change.

``embedding_id`` is the FK to the pgvector embedding row. NULL until
the embedding pass runs.

External-cache-shaped — survives a paper reset.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class TranscriptParagraph(Base):
    __tablename__ = "transcript_paragraphs"
    __table_args__ = (
        Index("ix_transcript_paragraphs_transcript_idx",
              "transcript_id", "paragraph_index"),
        Index("ix_transcript_paragraphs_ticker_period",
              "ticker", "fiscal_year", "fiscal_quarter"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transcript_id: Mapped[int] = mapped_column(
        ForeignKey("earnings_transcripts.id", ondelete="CASCADE"),
        index=True,
    )
    # Denormalized for cheap filter without JOIN. Always set to the
    # parent transcript's value at write time.
    ticker: Mapped[str] = mapped_column(String, index=True)
    fiscal_year: Mapped[int] = mapped_column(Integer)
    fiscal_quarter: Mapped[int] = mapped_column(Integer)
    # 0-based position within the call. Lets the embedding pipeline
    # reconstruct narrative order from a vector search hit.
    paragraph_index: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # AlphaVantage exposes ``title`` (e.g. "CFO", "Analyst, Goldman").
    speaker_title: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    # Set by Agent 4's vector pipeline once embedded. NULL = pending.
    embedding_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "transcript_id": self.transcript_id,
            "ticker": self.ticker,
            "fiscal_year": self.fiscal_year,
            "fiscal_quarter": self.fiscal_quarter,
            "paragraph_index": self.paragraph_index,
            "speaker": self.speaker,
            "speaker_title": self.speaker_title,
            "content": self.content,
            "embedding_id": self.embedding_id,
            "fetched_at": (self.fetched_at.isoformat()
                            if self.fetched_at else None),
        }
