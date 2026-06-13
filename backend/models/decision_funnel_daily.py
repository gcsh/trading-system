"""MITS Phase 18-FU Stream A — Decision Funnel daily rollup.

One row per calendar date (yyyy-mm-dd, ET) snapshot of the 10-stage
decision funnel + counterfactual histogram + cooldown audit. Persisted
nightly at 21:55 ET (BEFORE the 22:00 18.A attribution job so its
nightly rollups can read fresh funnel context). Manual recompute is
also exposed via ``POST /learning/funnel/recompute``.

Lifecycle on a paper-state reset: ``decision_funnel_daily`` is a
derived daily analytics rollup over ``decision_provenance`` +
``policy_rule_evaluations`` + Trade rows. It survives ``fresh_start``
(same semantics as ``learned_attribution`` / ``knowledge_graph_history``)
— the operator wants to keep historical funnel snapshots across
trial resets so cross-trial diagnostics stay intact. Listed in
``EXTERNAL_CACHE_TABLES`` (not ``PAPER_STATE_TABLES``) so future
readers understand the intent.

The 12 numeric stage counters answer "how many decisions reached each
stage" + 2 cooldown counters. ``payload_json`` carries the full
``FunnelReport.to_dict()`` projection so the API surface can rehydrate
the cohort histograms (confidence + counterfactual + cooldown sample
tickers) without re-running the compute.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    Date, DateTime, Float, Index, Integer, String,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class DecisionFunnelDaily(Base):
    __tablename__ = "decision_funnel_daily"
    __table_args__ = (
        Index("ix_dfd_date", "date", unique=True),
        Index("ix_dfd_computed_at", "computed_at"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    date: Mapped[date] = mapped_column(Date, index=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True,
    )
    # Watchlist size at compute time — anchor for "we evaluated N of W".
    watchlist_size: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    # The 10 funnel stage counters. All nullable so a partial / cold-
    # start row can land without fabricating zeros where the upstream
    # data is genuinely absent.
    n_evaluations: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    n_analysis_candidate: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    n_brain_non_hold: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    n_policy_eligible: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    n_consensus_quorum_met: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    n_consensus_non_abstain: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    n_risk_passed: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    n_simulator_passed: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    n_submitted: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    # Fill count is best-effort — we count trades where price * quantity
    # > 0 within the window (real fills); for a paper bot the parity is
    # ~1:1 with n_submitted, but the column is here so a future broker
    # integration can diverge them honestly.
    n_filled: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    n_closed_with_pnl: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    # Cooldown audit — n_cooldown_hits is the raw firings from
    # policy_rule_evaluations; n_cooldown_lost_opportunities is the
    # subset where a historical setup of ≥ threshold confidence was
    # active inside the cooldown window.
    n_cooldown_hits: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    n_cooldown_lost_opportunities: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    # Optional rollup KPI — composite quality mean for this date's
    # provenance rows, when decision_quality_score_json is populated.
    composite_quality_mean: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
    )
    # Compact projections so the cockpit doesn't have to re-decode the
    # full payload on every read.
    confidence_histogram_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    top_3_blockers_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    # Full FunnelReport.to_dict() snapshot.
    payload_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    # Honesty flags from the compute (e.g.
    # ``stage_2_analysis_candidate_sparse``, ``cooldown_audit_limited``)
    # so the consumer UI can render "limited diagnostic" warnings
    # instead of silently misleading the operator.
    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "date": (self.date.isoformat() if self.date else None),
            "computed_at": (
                self.computed_at.isoformat() if self.computed_at else None
            ),
            "watchlist_size": self.watchlist_size,
            "n_evaluations": self.n_evaluations,
            "n_analysis_candidate": self.n_analysis_candidate,
            "n_brain_non_hold": self.n_brain_non_hold,
            "n_policy_eligible": self.n_policy_eligible,
            "n_consensus_quorum_met": self.n_consensus_quorum_met,
            "n_consensus_non_abstain": self.n_consensus_non_abstain,
            "n_risk_passed": self.n_risk_passed,
            "n_simulator_passed": self.n_simulator_passed,
            "n_submitted": self.n_submitted,
            "n_filled": self.n_filled,
            "n_closed_with_pnl": self.n_closed_with_pnl,
            "n_cooldown_hits": self.n_cooldown_hits,
            "n_cooldown_lost_opportunities": (
                self.n_cooldown_lost_opportunities
            ),
            "composite_quality_mean": self.composite_quality_mean,
            "confidence_histogram_json": self.confidence_histogram_json,
            "top_3_blockers_json": self.top_3_blockers_json,
            "payload_json": self.payload_json,
            "notes": self.notes,
        }
