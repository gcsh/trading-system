"""MITS Phase 3 — Detector configuration (operator-facing toggle / params).

One row per detector name. The detector registry lives in code
(``backend.bot.detectors``) — this table records the OPERATOR'S overrides
on top of that registry:

  * `enabled` — when False, the detector is masked from
    `detect_all`, `recompute_cells`, and `load_knowledge_evidence`. The
    detector's previously-persisted observations stay in the database
    so a re-enable just unmasks them (no data loss).
  * `params_json` — JSON-encoded param overrides. Merged on top of each
    detector's `default_params()` at detection time.
  * `source` — `builtin` for code-registered detectors, `pine_import`
    for any imported via the Pine translator.
  * `pine_source` — original Pine script text for audit / re-translation.

UPSERT-keyed on `name`. Idempotent.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class DetectorConfig(Base):
    __tablename__ = "detector_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # JSON-encoded dict of param overrides. Merged on top of the
    # detector's `default_params()` at call time.
    params_json: Mapped[str] = mapped_column(String, default="{}")
    # 'builtin' | 'pine_import'.
    source: Mapped[str] = mapped_column(String, default="builtin", index=True)
    # Original Pine source text for pine_import-sourced detectors.
    pine_source: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    def to_dict(self) -> dict:
        import json as _json
        try:
            params = _json.loads(self.params_json or "{}")
        except Exception:
            params = {}
        return {
            "id": self.id,
            "name": self.name,
            "enabled": bool(self.enabled),
            "params": params,
            "source": self.source,
            "pine_source": self.pine_source,
            "last_updated_at": (
                self.last_updated_at.isoformat()
                if self.last_updated_at else None
            ),
        }
