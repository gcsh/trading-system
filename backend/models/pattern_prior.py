"""MITS Phase 0 — Pattern Prior (academic / external Bayesian prior).

External cohort priors loaded once at startup. The knowledge aggregator
applies these as Bayesian shrinkage weights when the per-cohort sample
size is small.

Each row is "for pattern X in cohort Y (free-form descriptor), the prior
win rate is P, weighted as W pseudo-observations, sourced from Z".

Idempotent: `load_default_priors` upserts on (pattern, cohort_descriptor).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime, Float, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class PatternPrior(Base):
    __tablename__ = "pattern_priors"
    __table_args__ = (
        UniqueConstraint(
            "pattern", "cohort_descriptor",
            name="uq_pattern_priors_pattern_cohort",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pattern: Mapped[str] = mapped_column(String, index=True)
    # Free-form cohort descriptor — e.g. "trending_up", "any", "high_iv".
    # Matched loosely by the aggregator (exact or "any" fallback).
    cohort_descriptor: Mapped[str] = mapped_column(String, default="any")
    prior_win_rate: Mapped[float] = mapped_column(Float, default=0.5)
    # Pseudo-observation count for Bayesian shrinkage. Higher = stronger prior.
    prior_weight: Mapped[int] = mapped_column(Integer, default=20)
    source: Mapped[str] = mapped_column(String, default="academic")
    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pattern": self.pattern,
            "cohort_descriptor": self.cohort_descriptor,
            "prior_win_rate": self.prior_win_rate,
            "prior_weight": self.prior_weight,
            "source": self.source,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
