"""MITS Phase 5 (P5.2) — prediction→outcome tracking row.

For every EOD-pass setup that the bot could have traded, we persist a
row here that gets resolved nightly:

  * traded_matched   — bot opened a trade in the predicted direction.
  * traded_diverged  — bot opened a trade BUT in a different direction
                          (e.g. predicted long_call but executed short_put).
  * not_traded       — bot did not act; ``skip_reason`` cites the gate
                          (catalyst_gate / thin_volume / regime_shift /
                          consensus_abstain / low_grade / etc).
  * pending          — bot acted but the position is still open at
                          reconcile time; will be resolved on a later run.
  * unresolved       — could not determine outcome (rare; data integrity).

This is an EXTERNAL-CACHE-shaped table (derived from public corpus +
realized trades). Survives ``fresh_start`` so the operator can audit
prediction accuracy across resets.
"""
from __future__ import annotations

from datetime import date as _date, datetime
from typing import Optional

from sqlalchemy import (
    Date, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


# Canonical outcome strings — kept as plain strings (not enum) so SQLite
# stores them readably and migrations are painless.
OUTCOME_PENDING = "pending"
OUTCOME_TRADED_MATCHED = "traded_matched"
OUTCOME_TRADED_DIVERGED = "traded_diverged"
OUTCOME_NOT_TRADED = "not_traded"
OUTCOME_UNRESOLVED = "unresolved"


class EodPredictionOutcome(Base):
    __tablename__ = "eod_prediction_outcomes"
    __table_args__ = (
        # One outcome row per (eod_analysis_id, ticker). When the prediction
        # row gets re-upserted (EOD pass re-run mid-day) the unique constraint
        # makes the reconcile idempotent.
        UniqueConstraint(
            "eod_analysis_id", "ticker",
            name="uq_eod_pred_outcome_analysis_ticker",
        ),
        Index("ix_eod_pred_outcome_date", "analysis_date"),
        Index("ix_eod_pred_outcome_ticker_date", "ticker", "analysis_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    eod_analysis_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("eod_analysis.id"), index=True,
    )
    ticker: Mapped[str] = mapped_column(String, index=True)
    analysis_date: Mapped[_date] = mapped_column(Date, index=True)

    predicted_direction: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)
    predicted_strike: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)
    predicted_dte: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True)
    # Cached at prediction time so a later corpus refresh can't rewrite
    # what the bot was looking at when it decided.
    posterior: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)
    sample_size: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True)
    rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    traded: Mapped[int] = mapped_column(Integer, default=0)
    trade_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("trades.id"), nullable=True, index=True,
    )
    actual_direction: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)
    actual_strike: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)
    actual_pnl_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)
    actual_pnl_dollars: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)

    outcome: Mapped[str] = mapped_column(
        String, default=OUTCOME_PENDING, index=True)
    skip_reason: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "eod_analysis_id": self.eod_analysis_id,
            "ticker": self.ticker,
            "analysis_date": (
                self.analysis_date.isoformat() if self.analysis_date else None
            ),
            "predicted_direction": self.predicted_direction,
            "predicted_strike": self.predicted_strike,
            "predicted_dte": self.predicted_dte,
            "posterior": self.posterior,
            "sample_size": self.sample_size,
            "rank": self.rank,
            "traded": bool(self.traded),
            "trade_id": self.trade_id,
            "actual_direction": self.actual_direction,
            "actual_strike": self.actual_strike,
            "actual_pnl_pct": self.actual_pnl_pct,
            "actual_pnl_dollars": self.actual_pnl_dollars,
            "outcome": self.outcome,
            "skip_reason": self.skip_reason,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "resolved_at": (
                self.resolved_at.isoformat() if self.resolved_at else None
            ),
        }
