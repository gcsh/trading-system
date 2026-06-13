"""MITS Phase 9 — operator-edited theory annotation overlay.

The auto-generated ``TheoryAnnotation`` (lines/markers/zones produced
by ``backend/bot/theories``) is one click away from being wrong about
something. The operator can drag endpoints in the Theory Studio and
press "Save" — at which point we persist the resulting JSON blob keyed
on ``(theory, ticker, window)``.

We don't normalise the structure: the row stores the same JSON shape
the front-end uses. The contract is the ``TheoryAnnotation`` schema in
``backend/bot/theories/schema.py``.

Listed in ``EXTERNAL_CACHE_TABLES`` in
``backend/bot/system_reset.py`` — operator-curated drawings, survive
paper-reset.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class SavedTheoryAnnotation(Base):
    __tablename__ = "saved_theory_annotations"
    __table_args__ = (
        UniqueConstraint(
            "theory", "ticker", "window",
            name="uq_saved_theory_annotation_key",
        ),
        Index("ix_saved_theory_annotation_ticker", "ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    theory: Mapped[str] = mapped_column(String, index=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    window: Mapped[str] = mapped_column(String, default="5y")
    annotation_json: Mapped[str] = mapped_column(String, default="{}")
    created_by: Mapped[str] = mapped_column(String, default="operator")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    def to_dict(self) -> dict:
        import json as _json
        try:
            payload = _json.loads(self.annotation_json or "{}")
        except Exception:
            payload = {}
        return {
            "id": self.id,
            "theory": self.theory,
            "ticker": self.ticker,
            "window": self.window,
            "annotation": payload,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
