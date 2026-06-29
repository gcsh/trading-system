"""MITS Phase 18-FU Stream D (Gap 10) — Learning Impact Measurement.

When a learning event happens (a weight set was applied, a policy
threshold flipped) the operator wants to answer ONE question: did the
system behave differently afterward? Until Stream D shipped, there was
no rolling before/after comparison. The advisor would persist a
recommendation, the operator would maybe approve it, and… nothing
explicit tied the post-apply trajectory back to the event.

This table is the answer. ONE row per (learning_event_type, event_id)
+ computed_at: a snapshot of pre/post-event composite quality
distribution + submission rate + closed-trade hit-rate over symmetric
windows around the event. The scheduler computes new rows nightly so
the cockpit can render "weight apply #42 lifted composite mean by 3
points (n=18 trades, marked insufficient sample)".

Honesty rules (enforced by the writer):
  * ``is_significant`` is 0 unless ``min(n_before, n_after)`` clears
    a configurable floor. With current sparsity (~2 closures / 14d) it
    will almost always be 0 — and the ``note`` field will say so out
    loud. The operator never sees a fake "significant lift".
  * Same window length on both sides (default 7d). Asymmetric windows
    would bias the delta — Stream D refuses to compute them.

Lifecycle on a paper-state reset: learning_impact is a derived audit
trail; like ``learned_attribution`` + ``policy_tunings`` +
``agent_weight_history`` it MUST survive ``fresh_start()`` so the
operator can review "what did the system do after I flipped the apply
flag last trial?" across trial restarts. Stream D does NOT modify
``backend.bot.system_reset``; the default is "table not listed = not
wiped", which matches the intent here. Documented for the next reader.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


# Canonical event-type strings — keep in sync with the impact_measurement
# module so the writer never invents a label the reader doesn't know.
EVENT_TYPE_WEIGHT_APPLY = "weight_apply"
EVENT_TYPE_POLICY_APPLY = "policy_apply"
EVENT_TYPE_WEIGHT_ROLLBACK = "weight_apply_rollback"
ALLOWED_EVENT_TYPES = (
    EVENT_TYPE_WEIGHT_APPLY,
    EVENT_TYPE_POLICY_APPLY,
    EVENT_TYPE_WEIGHT_ROLLBACK,
)


class LearningImpact(Base):
    __tablename__ = "learning_impact"
    __table_args__ = (
        Index("ix_li_event_type", "learning_event_type"),
        Index("ix_li_event", "learning_event_type", "event_id"),
        Index("ix_li_computed_at", "computed_at"),
        Index("ix_li_event_timestamp", "event_timestamp"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True,
    )
    learning_event_type: Mapped[str] = mapped_column(String, index=True)
    # FK-shape (no enforced relation since the source table varies per
    # event_type). Reader correlates back via (learning_event_type,
    # event_id).
    event_id: Mapped[int] = mapped_column(Integer, index=True)
    event_timestamp: Mapped[datetime] = mapped_column(
        DateTime, index=True,
    )
    before_window_days: Mapped[int] = mapped_column(
        Integer, default=7,
    )
    after_window_days: Mapped[int] = mapped_column(
        Integer, default=7,
    )
    # Per-metric dicts persisted as JSON-encoded strings so future
    # additions (e.g. mean_brier) don't require a schema change.
    metrics_before_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    metrics_after_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    delta_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    # SQLite has no boolean — stick with the project's int convention
    # (0 / 1) to keep parity with other learning tables (operator_reviewed
    # / operator_approved on policy_tunings et al).
    is_significant: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "computed_at": (
                self.computed_at.isoformat() if self.computed_at else None
            ),
            "learning_event_type": self.learning_event_type,
            "event_id": int(self.event_id) if self.event_id is not None else None,
            "event_timestamp": (
                self.event_timestamp.isoformat()
                if self.event_timestamp else None
            ),
            "before_window_days": int(self.before_window_days or 0),
            "after_window_days": int(self.after_window_days or 0),
            "metrics_before_json": self.metrics_before_json,
            "metrics_after_json": self.metrics_after_json,
            "delta_json": self.delta_json,
            "is_significant": int(self.is_significant or 0),
            "note": self.note,
        }
