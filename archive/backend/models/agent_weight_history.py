"""MITS Phase 18.D — Online Agent Weight Adaptation (Advisory).

Append-only history table. Every recompute writes one row per agent so
the operator can roll back to any prior weight set (and the engine,
when ``adaptive_weights_apply_enabled`` is True, reads the latest row
per agent rather than the hardcoded AGENT_FUNCS / per-vote weights).

ADVISORY by default. The advisory flag (``adaptive_weights_advisory_enabled``)
gates whether the nightly job persists rows. A SEPARATE flag
(``adaptive_weights_apply_enabled``) gates whether the engine actually
reads from this table. The operator MUST flip both, in order, before
adaptive weights influence a real cycle. Until then this table is
inert telemetry.

Lifecycle on a paper-state reset: this table is a long-running learning
artifact like ``learned_attribution`` and ``policy_tunings`` — survives
``fresh_start()`` so the operator doesn't lose accumulated calibration.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class AgentWeightHistory(Base):
    __tablename__ = "agent_weight_history"
    __table_args__ = (
        Index("ix_awh_agent", "agent"),
        Index("ix_awh_computed_at", "computed_at"),
        Index("ix_awh_agent_computed", "agent", "computed_at"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True,
    )
    agent: Mapped[str] = mapped_column(String, index=True)
    base_weight: Mapped[float] = mapped_column(Float)
    weight_proposed: Mapped[float] = mapped_column(Float)
    # ``weight_active`` is what the engine WILL use IF
    # ``adaptive_weights_apply_enabled`` is True. When apply is False the
    # engine ignores this row entirely; the value still gets written so
    # the operator can audit "what would have happened if apply was on."
    weight_active: Mapped[float] = mapped_column(Float)
    adaptive_multiplier: Mapped[float] = mapped_column(Float)
    n_closed: Mapped[int] = mapped_column(Integer, default=0)
    confidence_level: Mapped[str] = mapped_column(
        String, default="insufficient_data",
    )
    rationale: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    payload_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    # Operator review state — stays 0 / NULL until the 18.E hypothesis
    # studio wires the review UI. 18.D only writes the advisor rows.
    operator_reviewed: Mapped[int] = mapped_column(Integer, default=0)
    operator_approved: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "computed_at": (
                self.computed_at.isoformat() if self.computed_at else None
            ),
            "agent": self.agent,
            "base_weight": float(self.base_weight),
            "weight_proposed": float(self.weight_proposed),
            "weight_active": float(self.weight_active),
            "adaptive_multiplier": float(self.adaptive_multiplier),
            "n_closed": int(self.n_closed or 0),
            "confidence_level": self.confidence_level,
            "rationale": self.rationale,
            "payload_json": self.payload_json,
            "operator_reviewed": int(self.operator_reviewed or 0),
            "operator_approved": int(self.operator_approved or 0),
        }
