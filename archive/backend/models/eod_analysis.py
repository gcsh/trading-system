"""MITS Phase 3 — End-of-Day per-ticker analysis row.

`run_eod_pass()` populates one row per (ticker, analysis_date). Each row
encodes:

  * Patterns that fired during the day (JSON array of pattern names).
  * Top posterior cohort win-rate + the pattern that scored it.
  * AI-composed thesis paragraph (single Claude call per row).
  * Suggested action JSON (action, strike, dte, target, stop, rationale)
    — only populated when posterior > threshold AND sample_size floor.
  * Invalidation rules JSON (list of plain-English bullets).
  * `rank_score` = `posterior * log(1 + sample_size)` for ordering
    Tomorrow's Setup.

Idempotent UPSERT on (ticker, analysis_date) so re-running the EOD pass
the same day overwrites the row instead of duplicating.
"""
from __future__ import annotations

from datetime import date as _date, datetime
from typing import Optional

from sqlalchemy import (
    Date, DateTime, Float, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class EodAnalysis(Base):
    __tablename__ = "eod_analysis"
    __table_args__ = (
        UniqueConstraint("ticker", "analysis_date",
                          name="uq_eod_analysis_ticker_date"),
        Index("ix_eod_analysis_date", "analysis_date"),
        Index("ix_eod_analysis_rank", "rank_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    analysis_date: Mapped[_date] = mapped_column(Date, index=True)

    # JSON-encoded array of pattern names that fired that day.
    patterns_fired: Mapped[str] = mapped_column(String, default="[]")

    top_pattern: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    top_posterior: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    top_sample_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    thesis_paragraph: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    headline: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # JSON: {action, strike, dte, target_premium_pct, stop_premium_pct,
    #         rationale}. None when posterior < threshold or N < min_samples.
    suggested_action_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # JSON array of plain-English bullets.
    invalidation_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    rank_score: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        import json as _json
        try:
            patterns = _json.loads(self.patterns_fired or "[]")
        except Exception:
            patterns = []
        try:
            suggested = _json.loads(self.suggested_action_json) \
                if self.suggested_action_json else None
        except Exception:
            suggested = None
        try:
            invalidation = _json.loads(self.invalidation_json) \
                if self.invalidation_json else []
        except Exception:
            invalidation = []
        return {
            "id": self.id,
            "ticker": self.ticker,
            "analysis_date": (
                self.analysis_date.isoformat()
                if self.analysis_date else None
            ),
            "patterns_fired": patterns,
            "top_pattern": self.top_pattern,
            "top_posterior": self.top_posterior,
            "top_sample_size": self.top_sample_size,
            "confidence": self.confidence,
            "headline": self.headline,
            "thesis_paragraph": self.thesis_paragraph,
            "suggested_action": suggested,
            "invalidation": invalidation,
            "rank_score": self.rank_score,
            "created_at": (
                self.created_at.isoformat()
                if self.created_at else None
            ),
        }
