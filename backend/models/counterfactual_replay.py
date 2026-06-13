"""MITS Phase 18.B — Counterfactual replay cache.

Counterfactuals are deterministic functions of the immutable
``decision_provenance`` row (+ for the sizing variation, the linked
Trade.sizing_chain_json which is written once at fill time and never
updated). Caching the result lets the cockpit's What-if panel return
instantly on a re-open without re-running the aggregation primitive.

Cache key shape (``variation_key`` is variation-specific):
  * sizing      → ``"factors=0.5,1.0,1.5,2.0"``
  * policy      → ``"rule=correlation_cap_block"``
  * consensus   → ``"agent=simulator->buy@70"``

One row per (provenance_id, variation_kind, variation_key). New
counterfactuals append fresh rows — we never mutate an existing row,
so the operator can browse historical What-if asks the same way they
browse historical attribution batches.

Lifecycle on a paper-state reset: the cache is bot-derived (computed
from decision_provenance + trades). Both source tables are wiped on
fresh_start, so the cache is too — listed in ``PAPER_STATE_TABLES``
in ``backend.bot.system_reset``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class CounterfactualReplay(Base):
    __tablename__ = "counterfactual_replays"
    __table_args__ = (
        Index("ix_cfr_prov", "provenance_id"),
        Index("ix_cfr_kind", "variation_kind"),
        Index(
            "ix_cfr_lookup",
            "provenance_id", "variation_kind", "variation_key",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True,
    )
    provenance_id: Mapped[int] = mapped_column(Integer, index=True)
    variation_kind: Mapped[str] = mapped_column(String, index=True)
    variation_key: Mapped[str] = mapped_column(String, index=True)
    result_json: Mapped[Optional[str]] = mapped_column(
        String, nullable=True,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "computed_at": (
                self.computed_at.isoformat() if self.computed_at else None
            ),
            "provenance_id": self.provenance_id,
            "variation_kind": self.variation_kind,
            "variation_key": self.variation_key,
            "result_json": self.result_json,
        }
