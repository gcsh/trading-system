"""MITS Phase 0 — Knowledge Graph Cell.

One row per (ticker, pattern, regime, vol_state, time_bucket, horizon)
cohort, holding the aggregated statistics the AI Brain queries before
every decision:

    "When Bull Flag fires on NVDA in trending_up + low_vol +
     morning_session, looking 1d forward — historical sample N=347,
     win rate 71%, posterior win rate (Bayesian shrinkage with
     prior_weight 20) 68%, avg return +2.9%, Wilson 95% CI
     [62%, 74%]."

`recompute_cells` recomputes the table from
`market_observations + market_outcomes` and is idempotent (UPSERT on the
composite unique key).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime, Float, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class KnowledgeGraphCell(Base):
    __tablename__ = "knowledge_graph"
    __table_args__ = (
        # MITS Phase 1 — added `sample_split` to the cohort key so we can
        # store in-sample (historical_replay), out-of-sample (live), and
        # combined cells for the same axes.
        UniqueConstraint(
            "ticker", "pattern", "regime", "vol_state", "time_bucket",
            "horizon", "sample_split",
            name="uq_kg_cohort",
        ),
        Index("ix_kg_ticker_pattern", "ticker", "pattern"),
        Index("ix_kg_horizon", "horizon"),
        Index("ix_kg_sample_split", "sample_split"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    pattern: Mapped[str] = mapped_column(String, index=True)
    regime: Mapped[str] = mapped_column(String, default="unknown")
    vol_state: Mapped[str] = mapped_column(String, default="normal")
    time_bucket: Mapped[str] = mapped_column(String, default="rth")
    horizon: Mapped[str] = mapped_column(String, default="1d")
    # MITS Phase 1 — 'in_sample' | 'out_of_sample' | 'combined'.
    # `in_sample` rows aggregate over historical_replay observations.
    # `out_of_sample` rows aggregate over live_engine observations.
    # `combined` rows include every observation regardless of source.
    sample_split: Mapped[str] = mapped_column(String, default="combined",
                                                              index=True)

    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    posterior_win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_return_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_hold_minutes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence_lower: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence_upper: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # MITS Phase 13 Fix 7 — direction-aware CIs. Populated only when
    # the cell has BOTH long and short observations; otherwise stays
    # NULL (consumers read confidence_lower/upper instead).
    confidence_lower_long: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence_upper_long: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence_lower_short: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence_upper_short: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # MITS Phase 13 Fix 4 — hierarchical parent persistence.
    #   "cell"                — normal (ticker, pattern, regime, ...) cohort
    #   "pattern_regime_parent" — sentinel ticker="__ALL__", pooled across tickers
    #   "pattern_parent"      — sentinel ticker="__ALL__" + regime="__ALL__"
    parent_type: Mapped[str] = mapped_column(
        String, default="cell", nullable=False, index=True,
    )
    # MITS Phase 12.H — Hierarchical Bayesian shrinkage:
    #   "high"   N >= 100
    #   "medium" 30 <= N < 100
    #   "low"    10 <= N < 30
    #   "thin"   N < 10
    # Consumers (agent_context, EOD, theory engine) filter by this flag
    # via `min_n_for_action` so thin cells stay out of decision paths.
    confidence_level: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, default="thin", index=True,
    )

    last_updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "pattern": self.pattern,
            "regime": self.regime,
            "vol_state": self.vol_state,
            "time_bucket": self.time_bucket,
            "horizon": self.horizon,
            "sample_split": self.sample_split,
            "sample_size": self.sample_size,
            "win_rate": self.win_rate,
            "posterior_win_rate": self.posterior_win_rate,
            "avg_return_pct": self.avg_return_pct,
            "avg_hold_minutes": self.avg_hold_minutes,
            "confidence_lower": self.confidence_lower,
            "confidence_upper": self.confidence_upper,
            "confidence_lower_long": self.confidence_lower_long,
            "confidence_upper_long": self.confidence_upper_long,
            "confidence_lower_short": self.confidence_lower_short,
            "confidence_upper_short": self.confidence_upper_short,
            "confidence_level": self.confidence_level,
            "parent_type": self.parent_type,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
        }
