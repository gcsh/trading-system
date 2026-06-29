"""MITS Phase 14.D — Brain prediction → outcome ledger.

Every Claude composition that emerges from the hybrid composer (per
ticker × window × pattern) is stamped into this table at the time the
thesis is composed. A nightly linker job walks the pending rows, ties
each prediction to the trade that actually fired (if any), replays the
bars after the prediction was made to detect whether the model's
self-stated invalidation conditions tripped, and resolves the row into
{win, loss, scratch, not_traded}.

Separate from ``EodPredictionOutcome`` (which is EOD-only and keyed
to ``eod_analysis_id``); ``BrainPrediction`` is the broader ledger that
also captures the live ``/analysis/{ticker}`` and Opportunity Brain
surfaces.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index, Integer, String,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


OUTCOME_PENDING = "pending"
OUTCOME_WIN = "win"
OUTCOME_LOSS = "loss"
OUTCOME_SCRATCH = "scratch"
OUTCOME_NOT_TRADED = "not_traded"


class BrainPrediction(Base):
    __tablename__ = "brain_predictions"
    __table_args__ = (
        Index("ix_brain_pred_ticker_ts", "ticker", "created_at"),
        Index("ix_brain_pred_outcome", "outcome"),
        Index("ix_brain_pred_surface", "surface"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    surface: Mapped[str] = mapped_column(String)
    ticker: Mapped[str] = mapped_column(String, index=True)
    window: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    pattern: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    suggested_action: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)
    suggested_direction: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)
    suggested_strike: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)
    suggested_dte: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True)
    posterior_at_decision: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)
    sample_size_at_decision: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True)
    confidence_self_assessment: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)
    invalidation_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)
    thesis_paragraph: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True)

    linked_trade_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("trades.id"), nullable=True)
    actual_pnl_pct: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)
    invalidation_hit: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True)
    invalidation_saved_capital: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True)
    outcome: Mapped[str] = mapped_column(
        String, default=OUTCOME_PENDING, index=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True)

    # MITS Phase 15.E — Snapshots stamped at decision time (JSON blobs).
    regime_at_decision: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)
    confidence_breakdown_at_decision: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)
    top_strategy_at_decision: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)

    # MITS Phase 15.E — Per-component correctness (filled by nightly linker).
    regime_call_correct: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True)
    technical_call_correct: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True)
    options_call_correct: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True)
    analog_call_correct: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True)
    strategy_call_correct: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "surface": self.surface,
            "ticker": self.ticker,
            "window": self.window,
            "pattern": self.pattern,
            "suggested_action": self.suggested_action,
            "suggested_direction": self.suggested_direction,
            "suggested_strike": self.suggested_strike,
            "suggested_dte": self.suggested_dte,
            "posterior_at_decision": self.posterior_at_decision,
            "sample_size_at_decision": self.sample_size_at_decision,
            "confidence_self_assessment": self.confidence_self_assessment,
            "invalidation_json": self.invalidation_json,
            "thesis_paragraph": self.thesis_paragraph,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "linked_trade_id": self.linked_trade_id,
            "actual_pnl_pct": self.actual_pnl_pct,
            "invalidation_hit": self.invalidation_hit,
            "invalidation_saved_capital": self.invalidation_saved_capital,
            "outcome": self.outcome,
            "resolved_at": (
                self.resolved_at.isoformat() if self.resolved_at else None
            ),
            "regime_at_decision": self.regime_at_decision,
            "confidence_breakdown_at_decision": self.confidence_breakdown_at_decision,
            "top_strategy_at_decision": self.top_strategy_at_decision,
            "regime_call_correct": self.regime_call_correct,
            "technical_call_correct": self.technical_call_correct,
            "options_call_correct": self.options_call_correct,
            "analog_call_correct": self.analog_call_correct,
            "strategy_call_correct": self.strategy_call_correct,
        }
