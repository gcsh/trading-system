"""Stage-19 — Earnings call intelligence cache.

One row per 8-K item-2.02 filing analyzed. Carries structured
"what did management actually say?" signals — guidance change, margin
trajectory, tone, plus a few verbatim key quotes — so the Narrative
agent (and Mission Control) can read the actual story behind the
headlines.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class EarningsCallIntel(Base):
    __tablename__ = "earnings_call_intel"
    __table_args__ = (UniqueConstraint("ticker", "accession_number",
                                            name="uq_eci_ticker_accession"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    accession_number: Mapped[str] = mapped_column(String)
    filed_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    # Structured signals
    guidance_change: Mapped[str] = mapped_column(String, default="none")
    # improved | maintained | reduced | first_time | withdrawn | none
    margin_trajectory: Mapped[str] = mapped_column(String, default="n/a")
    # expanding | stable | contracting | n/a
    management_tone: Mapped[str] = mapped_column(String, default="neutral")
    # confident | cautious | mixed | neutral

    # Free-text artefacts the narrative agent + UI render
    key_quotes_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    forward_looking_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    source: Mapped[str] = mapped_column(String, default="heuristic")
    # heuristic | claude
    fetched_at: Mapped[datetime] = mapped_column(DateTime,
                                                       default=datetime.utcnow)

    def to_dict(self) -> dict:
        import json as _json
        try:
            key_quotes = _json.loads(self.key_quotes_json or "[]")
        except Exception:
            key_quotes = []
        try:
            forward_looking = _json.loads(self.forward_looking_json or "[]")
        except Exception:
            forward_looking = []
        return {
            "ticker": self.ticker,
            "accession_number": self.accession_number,
            "filed_at": self.filed_at.isoformat() if self.filed_at else None,
            "guidance_change": self.guidance_change,
            "margin_trajectory": self.margin_trajectory,
            "management_tone": self.management_tone,
            "key_quotes": key_quotes,
            "forward_looking": forward_looking,
            "summary": self.summary,
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }
