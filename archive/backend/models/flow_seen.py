"""Flowseeker: persisted set of alert IDs already pushed to clients.

Used to deduplicate flow alerts so a WebSocket reconnect does not replay old
alerts the user has already seen.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class SeenFlowAlert(Base):
    __tablename__ = "seen_flow_alerts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
