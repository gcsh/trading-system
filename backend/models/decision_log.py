"""Decision log — every actionable engine decision is recorded with its
analytical context (regime, grade, win probability, features) so we can later
ask: *which combinations of strategy + regime + grade actually make money?*

That feedback table is the substrate the trained ML probability/ranker layers
need; for now it lets us surface per-strategy / per-regime / per-grade insights
in the UI and detect failing combinations.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class DecisionLog(Base):
    __tablename__ = "decision_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)
    strategy: Mapped[str] = mapped_column(String, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String, default="signal_only", index=True)

    regime_trend: Mapped[str] = mapped_column(String, default="unknown")
    regime_volatility: Mapped[str] = mapped_column(String, default="normal")
    regime_gamma: Mapped[str] = mapped_column(String, default="unknown")
    regime_label: Mapped[str] = mapped_column(String, default="")

    grade: Mapped[str] = mapped_column(String, default="")
    win_probability: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Set when the decision led to an executed trade.
    trade_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # Outcome — recorded when the position closes.
    outcome_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    outcome_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    features_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # P1.1 — decision-poisoning root-cause column. Every analytic that
    # reads DecisionLog must filter by signal_source != "historical_replay"
    # before feeding live decision paths (gate calibration, agent scoring,
    # learning, attribution). Without this column we had to join through
    # Trade.signal_source — fragile + missed rows without trade_id.
    # Values: "live_engine" | "ai_brain" | "exit_manager" | "historical_replay"
    signal_source: Mapped[str] = mapped_column(String, default="live_engine",
                                                       index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "ticker": self.ticker, "action": self.action, "strategy": self.strategy,
            "confidence": self.confidence, "status": self.status,
            "regime_trend": self.regime_trend, "regime_volatility": self.regime_volatility,
            "regime_gamma": self.regime_gamma, "regime_label": self.regime_label,
            "grade": self.grade, "win_probability": self.win_probability,
            "trade_id": self.trade_id, "outcome_pnl": self.outcome_pnl,
            "outcome_status": self.outcome_status,
            "signal_source": self.signal_source,
        }
