"""MITS Phase 18.C — Policy Auto-Tuning (Advisory) recommendations.

One row per (rule_name, computed_at) recording the advisor's
recommended threshold for that rule + the per-bucket payload that
backs it. Recommendations are ADVISORY — the operator reviews via
the cockpit / Decision Hub and applies the change manually (the
18.C-future ``policy_tuning_auto_apply_enabled`` flag stays OFF
until the operator opts in to auto-apply).

The optional ``operator_reviewed`` + ``operator_approved`` + ``applied_at``
fields exist so the 18.E hypothesis studio can record the operator's
verdict on each recommendation without needing a separate ledger.
For 18.C they stay at 0 / NULL — only the advisor writes here.

Lifecycle on a paper-state reset: this table is derived (computed
from decision_provenance + trades), but it is a long-running learning
artifact like ``learned_attribution`` and ``knowledge_graph_history``.
We list it in ``EXTERNAL_CACHE_TABLES`` in ``backend.bot.system_reset``
so future readers understand the intent — fresh_start does NOT wipe
prior recommendations.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class PolicyTuning(Base):
    __tablename__ = "policy_tunings"
    __table_args__ = (
        Index("ix_pt_rule", "rule_name"),
        Index("ix_pt_computed_at", "computed_at"),
        Index("ix_pt_rule_computed", "rule_name", "computed_at"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True,
    )
    rule_name: Mapped[str] = mapped_column(String, index=True)
    threshold_attr: Mapped[str] = mapped_column(String)
    current_value: Mapped[float] = mapped_column(Float)
    recommended_value: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
    )
    recommendation_confidence: Mapped[str] = mapped_column(
        String, default="insufficient_data",
    )
    rationale: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    payload_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    # 18.E hypothesis-studio integration — operator review state.
    # 18.C only writes the advisor rows; these stay 0 / NULL until the
    # operator UI is wired in 18.E.
    operator_reviewed: Mapped[int] = mapped_column(Integer, default=0)
    operator_approved: Mapped[int] = mapped_column(Integer, default=0)
    applied_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "computed_at": (
                self.computed_at.isoformat() if self.computed_at else None
            ),
            "rule_name": self.rule_name,
            "threshold_attr": self.threshold_attr,
            "current_value": float(self.current_value),
            "recommended_value": (
                float(self.recommended_value)
                if self.recommended_value is not None else None
            ),
            "recommendation_confidence": self.recommendation_confidence,
            "rationale": self.rationale,
            "payload_json": self.payload_json,
            "operator_reviewed": int(self.operator_reviewed or 0),
            "operator_approved": int(self.operator_approved or 0),
            "applied_at": (
                self.applied_at.isoformat() if self.applied_at else None
            ),
        }
