"""Watchlist table — user-curated tickers with optional notes."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class WatchlistItem(Base):
    """A single watchlist entry. Multiple lists share this table via ``list_name``."""

    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("list_name", "ticker", name="uq_watchlist_list_ticker"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    list_name: Mapped[str] = mapped_column(String, default="default", index=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    notes: Mapped[str] = mapped_column(String, default="")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # When 1, the engine still evaluates this ticker for stock signals but
    # never asks the options pipeline for it. Set this on illiquid names
    # like WULF whose option chains return permanently wide spreads — the
    # integrity layer correctly rejects them every cycle, generating noise
    # but no useful data. Default 0 (options enabled).
    options_disabled: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "list_name": self.list_name,
            "ticker": self.ticker,
            "notes": self.notes,
            "added_at": self.added_at.isoformat() if self.added_at else None,
            "options_disabled": bool(self.options_disabled),
        }
