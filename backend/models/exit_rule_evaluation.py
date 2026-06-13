"""MITS Phase 17.E — per-rule exit policy evaluation ledger.

Mirrors :class:`backend.models.policy_rule_evaluation.PolicyRuleEvaluation`
for the exit-side declarative policy. Every ExitRule evaluation (fired
or not) writes one row at the moment the exit_manager judges a
position. The ``/exit/veto-budget`` endpoint aggregates by rule name +
window so the operator can see which exit triggers fire most often.

Rows persist regardless of whether the position actually closed — the
fire boolean carries that. Concurrent triggers (multiple rules
firing in one cycle) produce multiple ``fired=1`` rows joined by the
shared ``position_id`` + ``evaluated_at`` timestamp.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class ExitRuleEvaluation(Base):
    __tablename__ = "exit_rule_evaluations"
    __table_args__ = (
        Index("ix_exit_eval_rule_ts", "rule_name", "evaluated_at"),
        Index("ix_exit_eval_ticker_ts", "ticker", "evaluated_at"),
        Index("ix_exit_eval_position_ts", "position_id", "evaluated_at"),
        Index("ix_exit_eval_fired", "fired"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime, index=True, default=datetime.utcnow,
    )
    position_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    ticker: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    rule_name: Mapped[str] = mapped_column(String)
    severity: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    fired: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    legacy_action: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    evidence_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "evaluated_at": (
                self.evaluated_at.isoformat() if self.evaluated_at else None
            ),
            "position_id": self.position_id,
            "ticker": self.ticker,
            "rule_name": self.rule_name,
            "severity": self.severity,
            "fired": bool(self.fired),
            "legacy_action": self.legacy_action,
            "reason": self.reason,
            "evidence_json": self.evidence_json,
        }
