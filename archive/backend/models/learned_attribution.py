"""MITS Phase 18.A — Learned Hypothesis Attribution ledger.

One row per (scope_kind, scope_name, window_days, computed_at). Three
``scope_kind`` values today: ``agent``, ``axis``, ``strategy``. Every
field is a thin numeric projection of the dataclasses computed by
``backend.bot.learning.attribution``; the heavy nested payload lives
in ``payload_json`` so the API surface can rehydrate the full picture
without re-running the aggregator.

Lifecycle on a paper-state reset: ``learned_attribution`` is derived
from closed Trades + DecisionProvenance. The derived rows survive a
``fresh_start`` (mirroring ``knowledge_graph_history`` semantics) — the
calibration scoreboard is a long-running learning artifact, not bot
operating state. It is listed in ``EXTERNAL_CACHE_TABLES`` in
``backend.bot.system_reset`` so future readers understand the intent.

The ``notes`` column carries honesty flags from the aggregator:
``insufficient_sample_size_n_lt_<N>`` when ``n_closed`` is below the
``min_n`` guardrail, and ``stale_calibration`` when the oldest sample
referenced is older than 30 days. The point of these flags is so the
operator UI displays "we don't have enough data yet" instead of
showing a misleading point estimate.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    DateTime, Float, Index, Integer, String,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class LearnedAttribution(Base):
    __tablename__ = "learned_attribution"
    __table_args__ = (
        Index("ix_la_scope", "scope_kind", "scope_name"),
        Index("ix_la_computed_at", "computed_at"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True,
    )
    scope_kind: Mapped[str] = mapped_column(String, index=True)
    scope_name: Mapped[str] = mapped_column(String, index=True)
    window_days: Mapped[int] = mapped_column(Integer)
    n_closed: Mapped[int] = mapped_column(Integer, default=0)
    # All metric columns nullable so the "insufficient sample" path can
    # store a row that says "we tried, here's the honest answer: not
    # enough data" without fabricating a number.
    hit_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hit_rate_wilson_lower: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
    )
    hit_rate_wilson_upper: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
    )
    mean_pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    brier_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ece: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    spearman_corr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    discrimination: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
    )
    payload_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # MITS Phase 18.E — operator review state. Mirrors the same two
    # flags on policy_tunings + agent_weight_history so the hypothesis
    # studio can use a uniform approve/rollback workflow across all
    # three learning tables. 18.A only writes the advisor rows; these
    # stay 0 until the operator UI flips them via /learning/approve.
    operator_reviewed: Mapped[int] = mapped_column(Integer, default=0)
    operator_approved: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "computed_at": (
                self.computed_at.isoformat() if self.computed_at else None
            ),
            "scope_kind": self.scope_kind,
            "scope_name": self.scope_name,
            "window_days": self.window_days,
            "n_closed": self.n_closed,
            "hit_rate": self.hit_rate,
            "hit_rate_wilson_lower": self.hit_rate_wilson_lower,
            "hit_rate_wilson_upper": self.hit_rate_wilson_upper,
            "mean_pnl_pct": self.mean_pnl_pct,
            "brier_score": self.brier_score,
            "ece": self.ece,
            "spearman_corr": self.spearman_corr,
            "discrimination": self.discrimination,
            "payload_json": self.payload_json,
            "notes": self.notes,
            "operator_reviewed": int(self.operator_reviewed or 0),
            "operator_approved": int(self.operator_approved or 0),
        }
