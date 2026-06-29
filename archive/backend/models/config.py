"""Persistent bot configuration stored as a single JSON blob."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from backend.config import DEFAULT_BOT_CONFIG
from backend.db import Base


class BotConfig(Base):
    """Single-row table holding the live bot configuration as JSON."""

    __tablename__ = "bot_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    payload: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.payload)


def load_config(session: Session) -> dict[str, Any]:
    """Return the current config dict, seeding defaults on first call."""
    row = session.get(BotConfig, 1)
    if row is None:
        row = BotConfig(id=1, payload=json.dumps(DEFAULT_BOT_CONFIG))
        session.add(row)
        session.commit()
        return dict(DEFAULT_BOT_CONFIG)
    return row.to_dict()


def save_config(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist the given payload, replacing whatever was stored before."""
    row = session.get(BotConfig, 1)
    serialized = json.dumps(payload)
    if row is None:
        row = BotConfig(id=1, payload=serialized)
        session.add(row)
    else:
        row.payload = serialized
        row.updated_at = datetime.utcnow()
    session.commit()
    return payload
