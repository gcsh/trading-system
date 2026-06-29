"""MITS Phase 0 — Corpus Status (per-ticker bootstrap state).

When a ticker is added to the watchlist, a background job kicks off the
historical-replay pipeline. This table tracks where that pipeline is for
each ticker so the UI can show "building 47%", "ready", or "error".

One row per ticker; UPSERT on `ticker`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class CorpusStatus(Base):
    __tablename__ = "corpus_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, unique=True, index=True)

    # "pending" | "building" | "ready" | "insufficient" | "error"
    status: Mapped[str] = mapped_column(String, default="pending", index=True)

    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    outcome_count: Mapped[int] = mapped_column(Integer, default=0)
    cell_count: Mapped[int] = mapped_column(Integer, default=0)

    last_built_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # MITS Phase 2 (P2.5 walk-forward refinement). Earliest live_engine
    # observation timestamp for this ticker, recomputed by
    # `recompute_cells`. Observations BEFORE this cutoff are classified
    # as in_sample, observations on/after are out_of_sample. NULL means
    # the ticker has no live observations yet — falls back to
    # source-based splitting (Phase 1 behaviour).
    first_live_observation_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "status": self.status,
            "observation_count": self.observation_count,
            "outcome_count": self.outcome_count,
            "cell_count": self.cell_count,
            "last_built_at": self.last_built_at.isoformat() if self.last_built_at else None,
            "next_scheduled_at": self.next_scheduled_at.isoformat() if self.next_scheduled_at else None,
            "first_live_observation_at": (
                self.first_live_observation_at.isoformat()
                if self.first_live_observation_at else None
            ),
            "error": self.error,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
