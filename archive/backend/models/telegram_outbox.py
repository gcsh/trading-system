"""Persistent outbox for the Telegram notifier.

When the Telegram API is unreachable (429 rate limit, 5xx, or a flat
network error) the notifier enqueues the JSON payload here. A scheduler
job (`_telegram_drain_queue`, every 60s) re-attempts every eligible row.

Schema:
    id              autoincrement primary key
    payload         JSON-serialized sendMessage args
    attempt_count   number of HTTP attempts so far
    next_attempt_at next earliest time the row may be re-tried (UTC)
    created_at      first-enqueue timestamp (UTC)
    last_error      truncated repr of the most recent failure

Exponential backoff schedule (attempt → wait):
    1 → 30s
    2 → 2min
    3 → 10min
    4 → 1h
    5 → 6h
After ``max_attempts`` (default 5) the sweeper deletes the row.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class TelegramOutbox(Base):
    __tablename__ = "telegram_outbox"
    __table_args__ = (
        Index("ix_telegram_outbox_next", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # JSON-encoded payload — the literal dict we'll POST to sendMessage.
    payload: Mapped[str] = mapped_column(String, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow,
    )
    last_error: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "payload": self.payload,
            "attempt_count": self.attempt_count,
            "next_attempt_at": (self.next_attempt_at.isoformat()
                                 if self.next_attempt_at else None),
            "created_at": (self.created_at.isoformat()
                            if self.created_at else None),
            "last_error": self.last_error,
        }
