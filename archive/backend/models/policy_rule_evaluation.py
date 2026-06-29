"""MITS Phase 16.A — per-rule policy evaluation ledger.

Every PolicyRule evaluation (passed or blocked) writes one row. The
veto-budget endpoint aggregates by rule name + window; the
"Why didn't I trade?" UI joins by ticker + cycle_id to reconstruct the
full set of concurrent BlockingFactors for one decision.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class PolicyRuleEvaluation(Base):
    __tablename__ = "policy_rule_evaluations"
    __table_args__ = (
        Index("ix_policy_eval_rule_ts", "rule_name", "evaluated_at"),
        Index("ix_policy_eval_ticker_ts", "ticker", "evaluated_at"),
        Index("ix_policy_eval_blocked", "blocked"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    rule_name: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    ticker: Mapped[str] = mapped_column(String)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime, index=True, default=datetime.utcnow,
    )
    blocked: Mapped[bool] = mapped_column(Boolean, index=True)
    reason: Mapped[str] = mapped_column(String, default="")
    sizing_penalty_pct: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    cycle_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "rule_name": self.rule_name,
            "category": self.category,
            "severity": self.severity,
            "ticker": self.ticker,
            "evaluated_at": (
                self.evaluated_at.isoformat() if self.evaluated_at else None
            ),
            "blocked": bool(self.blocked),
            "reason": self.reason,
            "sizing_penalty_pct": float(self.sizing_penalty_pct or 0.0),
            "evidence_json": self.evidence_json,
            "cycle_id": self.cycle_id,
        }
