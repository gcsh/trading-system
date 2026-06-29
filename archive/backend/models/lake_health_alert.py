"""MITS Phase 9.5 — Lake health monitor alert row.

Hourly cron (``_lake_health_check`` in ``backend/bot/scheduler.py``)
samples ``/lake/status`` and writes a row whenever a configured
threshold trips. Rows are auto-resolved on the next check that finds
the condition cleared, or manually via the Lake Status UI's
"Acknowledge" button.

Listed in ``EXTERNAL_CACHE_TABLES`` — operator-visible alerts about a
shared lake; survive paper-reset.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class LakeHealthAlert(Base):
    __tablename__ = "lake_health_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Stable identifier for the failing rule, e.g.
    # "bronze_stale" / "gold_stale" / "vector_shrink" / "write_failures".
    kind: Mapped[str] = mapped_column(String, index=True)
    severity: Mapped[str] = mapped_column(String, default="warning")
    # JSON-encoded snapshot of the supporting context (ages, counts).
    detail_json: Mapped[str] = mapped_column(String, default="{}")

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True,
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True,
    )
    resolved_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    def to_dict(self) -> dict:
        import json as _json
        try:
            detail = _json.loads(self.detail_json or "{}")
        except Exception:
            detail = {}
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "detail": detail,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
        }
