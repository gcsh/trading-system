"""MITS Phase 18-FU Stream D (Gap 6) — Per-cycle Weight Application Log.

When 18.D's ``adaptive_weights_apply_enabled`` flag flips ON, the engine
calls ``get_current_weights()`` once per consensus cycle and overrides
the static per-vote weights with the live adaptive set. Until Stream D
shipped, that override happened silently — there was no per-cycle trail
of "for cycle X, the weights from history_id=Y were applied". Hard to
debug consensus drift if you can't tie a specific cycle to the exact
weight set it consumed.

This table is the forensic log. ONE row per cycle that actually applied
adaptive weights (the no-op fall-through to base weights doesn't write
a row — we'd flood the ledger with "default behavior" noise). The
``agent_weight_history_id`` column points back at the latest row per
agent that ``get_current_weights()`` sourced its values from; the
``weight_set_json`` column persists the exact ``{agent: weight}`` map
the engine consumed so the operator can replay any cycle's weight
context without joining back to ``agent_weight_history``.

Growth budget: this table grows ~1 row per consensus cycle when the
apply flag is on. With a 10-minute engine cycle running 16h/day that's
~96 rows/day. A 30-day rolling TTL keeps the table bounded; the prune
fires nightly from the scheduler (Stream D wires the job at 22:50 ET).

Lifecycle on a paper-state reset: this is bot-derived telemetry —
when the operator wipes trades + provenance, the per-cycle weight
log is consistent only if also wiped. But because Stream D does NOT
own ``backend.bot.system_reset``, the row stays alive across resets
(harmless, just stale). The 30-day TTL bounds the worst-case footprint
so leaving it across resets has no operational impact.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class WeightApplicationLog(Base):
    __tablename__ = "weight_application_log"
    __table_args__ = (
        Index("ix_wal_applied_at", "applied_at"),
        Index("ix_wal_cycle", "cycle_id"),
        Index("ix_wal_history", "agent_weight_history_id"),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    applied_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True,
    )
    # Cycle timestamp string — mirrors the engine's cycle_id convention
    # (event['timestamp'] ISO string). Nullable so calls outside the
    # cycle hook (tests, on-demand audit) can still write a row.
    cycle_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Latest ``agent_weight_history.id`` consulted at apply time. May be
    # None when no history row exists yet (apply_enabled flipped but
    # the advisor pass hasn't run) — in that case the engine fell back
    # to ``AGENT_BASE_WEIGHTS`` for every agent.
    agent_weight_history_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True, index=True,
    )
    # Snapshot of {agent: weight} the engine actually consumed this
    # cycle. Persisted as a JSON-encoded string so the audit row is
    # self-contained — no need to join back to agent_weight_history
    # to reconstruct the weight set.
    weight_set_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )
    # Optional link to a specific decision the weight applied to. The
    # engine writes None when the apply hook fires during a cycle that
    # ultimately abstains (no DecisionProvenance row produced); the
    # weight WAS applied to the consensus aggregation, just no
    # downstream provenance link exists.
    decision_provenance_id: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    # Operator-facing quality signal — the composite from the most
    # recent decision_quality_score_json. Helps Stream D's impact
    # measurement primitive correlate "applied weights X" → "downstream
    # composite Y". Nullable when no recent scorecard exists.
    composite_quality_at_apply: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "applied_at": (
                self.applied_at.isoformat() if self.applied_at else None
            ),
            "cycle_id": self.cycle_id,
            "agent_weight_history_id": self.agent_weight_history_id,
            "weight_set_json": self.weight_set_json,
            "decision_provenance_id": self.decision_provenance_id,
            "composite_quality_at_apply": (
                float(self.composite_quality_at_apply)
                if self.composite_quality_at_apply is not None else None
            ),
        }
