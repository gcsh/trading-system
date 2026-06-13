"""MITS Phase 18.E — Learning Rollback Log (operator audit trail).

Append-only ledger of every operator approve/rollback action taken
against a learning advisory row (``learned_attribution``,
``policy_tunings``, ``agent_weight_history``). The studio writes one
row per action; the row captures the target table + row id, the
action verb, optional operator notes, and a JSON snapshot of the
target row's state at the moment of the action.

The point of this table is non-repudiation. The 18.C / 18.D / 18.A
advisory tables already carry an ``operator_reviewed`` /
``operator_approved`` flag; if all we ever did was flip those flags
the history of "who decided what when, and what did the row look like
at the time" would vanish on the next advisory pass. This ledger keeps
that history intact, indexed by table + row + created_at so the
hypothesis studio can render the per-row audit ribbon without
scanning the world.

Lifecycle on a paper-state reset: long-running operator artifact —
survives ``fresh_start()`` like ``learned_attribution`` and
``policy_tunings``. Listed in ``EXTERNAL_CACHE_TABLES`` in
``backend.bot.system_reset`` so future readers understand the intent.

NOTE: ``operator`` is a hardcoded ``'operator'`` placeholder today.
The single-operator paper-trading system has no auth (matches the
existing UI pattern). The column exists so a future multi-user shim
can populate it without a migration.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


# Allowed values for ``action`` — strings rather than an Enum so SQLite
# stays simple and future-extension (e.g. ``"reset"``) needs no schema
# migration. Validation lives at the route layer.
ACTION_APPROVE = "approve"
ACTION_ROLLBACK = "rollback"
ALLOWED_ACTIONS = (ACTION_APPROVE, ACTION_ROLLBACK)

# Allowed target tables — keep aligned with the 3 learning tables 18.E
# governs. The route layer rejects anything else with 400 so we never
# write an orphan audit row that references a table the studio can't
# render.
TABLE_LEARNED_ATTRIBUTION = "learned_attribution"
TABLE_POLICY_TUNINGS = "policy_tunings"
TABLE_AGENT_WEIGHT_HISTORY = "agent_weight_history"
ALLOWED_TABLES = (
    TABLE_LEARNED_ATTRIBUTION,
    TABLE_POLICY_TUNINGS,
    TABLE_AGENT_WEIGHT_HISTORY,
)


class LearningRollbackLog(Base):
    __tablename__ = "learning_rollback_log"
    __table_args__ = (
        Index("ix_lrl_created_at", "created_at"),
        Index("ix_lrl_table", "table_name"),
        Index("ix_lrl_row", "row_id"),
        Index("ix_lrl_action", "action"),
        Index(
            "ix_lrl_table_row_created",
            "table_name", "row_id", "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True,
    )
    table_name: Mapped[str] = mapped_column(String, index=True)
    row_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String, index=True)
    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    operator: Mapped[str] = mapped_column(String, default="operator")
    snapshot_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created_at": (
                self.created_at.isoformat() if self.created_at else None
            ),
            "table_name": self.table_name,
            "row_id": int(self.row_id),
            "action": self.action,
            "notes": self.notes,
            "operator": self.operator,
            "snapshot_json": self.snapshot_json,
        }
