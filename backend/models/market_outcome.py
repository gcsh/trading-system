"""MITS Phase 0 — Market Outcome (forward return for an observation).

For every `MarketObservation`, the outcome linker writes one row per
horizon (5min/30min/60min/1d/5d/20d) capturing the *actual* forward
return after the pattern fired. This is the empirical evidence the
knowledge graph aggregates into per-cohort win rates.

Idempotency: unique on (observation_id, horizon) so re-running the
linker is safe.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class MarketOutcome(Base):
    __tablename__ = "market_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "observation_id", "horizon",
            name="uq_market_outcome_obs_horizon",
        ),
        Index("ix_market_outcome_horizon", "horizon"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    observation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("market_observations.id"), index=True,
    )
    # "5min" | "30min" | "60min" | "1d" | "5d" | "20d"
    horizon: Mapped[str] = mapped_column(String, index=True)

    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    return_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    was_winner: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    computed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "observation_id": self.observation_id,
            "horizon": self.horizon,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "return_pct": self.return_pct,
            "was_winner": self.was_winner,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
        }
