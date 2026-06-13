"""MITS Phase 16.B — decision provenance ledger.

Every consensus-bearing engine event writes one row here. The row
captures the full decision context — regime vector, strategy matrix,
typed agent input envelope, per-agent output projections, full
consensus block + chairman memo, policy result, simulator verdict,
correlation cap result, portfolio context — as 11 JSON columns. Each
column is independently nullable so a sparse row (e.g. blocked
post-consensus, no policy_result yet) still inserts cleanly.

The point is deterministic replay (see ``backend.bot.decision.replay``):
given a row id, the replay helper rebuilds AgentVote objects from
``agent_outputs_json`` and re-runs ``aggregate()`` with identical
parameters. If consensus.stance + confidence match, the round-trip is
lossless. Drift surfaces a concrete reproducibility bug.

Lifecycle on a paper-state reset: ``decision_provenance`` is bot-
generated decision history, so it is wiped via ``PAPER_STATE_TABLES``
in ``backend.bot.system_reset`` alongside trades + decision_log.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    DateTime, ForeignKey, Index, Integer, String,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class DecisionProvenance(Base):
    __tablename__ = "decision_provenance"
    __table_args__ = (
        Index("ix_dp_trade", "trade_id"),
        Index("ix_dp_ticker_ts", "ticker", "decision_timestamp"),
        Index("ix_dp_status", "event_status"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    trade_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("trades.id"), nullable=True,
    )
    event_status: Mapped[str] = mapped_column(String)
    ticker: Mapped[str] = mapped_column(String)
    decision_timestamp: Mapped[datetime] = mapped_column(
        DateTime, index=True, default=datetime.utcnow,
    )
    cycle_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    regime_vector_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    strategy_matrix_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    agent_inputs_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    agent_outputs_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    consensus_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    chairman_memo_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    policy_result_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    simulator_verdict_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    correlation_cap_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    portfolio_context_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    # MITS Phase 16.C — composite decision-quality score (analysis,
    # council, risk, execution + composite). Pure function of the row;
    # cached so /decision/scorecard never recomputes 50 rows on read.
    decision_quality_score_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )

    # MITS Phase 18-FU Gap 4 — provenance kind. Mirror of
    # ``trades.source_kind``; see trade.py docstring for full semantics.
    # Synthetic rows from ``backend.bot.learning.backfill`` are tagged
    # ``synthetic_backfill`` so learning-layer aggregators can exclude
    # them from default reads.
    source_kind: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, default="live", index=True,
    )

    # MITS Phase 19 — counterfactual "would-have-been" execution panel
    # for non-submitted rows. JSON envelope with fill_snapshot /
    # sizing_chain / chain_selection / exit_policy_result keys whose
    # values are plain-English insight strings (NOT replayable provenance
    # objects — the four ``Trade.*_json`` columns are still authoritative
    # for executed trades). Populated on every HOLD cycle by the engine
    # so the Decision Cockpit's execution panel surfaces meaningful
    # content instead of EmptyState. Read-only / observational; never
    # changes the policy outcome and never writes a trade row.
    would_have_been_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "trade_id": self.trade_id,
            "event_status": self.event_status,
            "ticker": self.ticker,
            "decision_timestamp": (
                self.decision_timestamp.isoformat()
                if self.decision_timestamp else None
            ),
            "cycle_id": self.cycle_id,
            "regime_vector_json": self.regime_vector_json,
            "strategy_matrix_json": self.strategy_matrix_json,
            "agent_inputs_json": self.agent_inputs_json,
            "agent_outputs_json": self.agent_outputs_json,
            "consensus_json": self.consensus_json,
            "chairman_memo_json": self.chairman_memo_json,
            "policy_result_json": self.policy_result_json,
            "simulator_verdict_json": self.simulator_verdict_json,
            "correlation_cap_json": self.correlation_cap_json,
            "portfolio_context_json": self.portfolio_context_json,
            "decision_quality_score_json": (
                self.decision_quality_score_json
            ),
            "source_kind": self.source_kind,
            "would_have_been_json": self.would_have_been_json,
        }
