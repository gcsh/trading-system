"""Local paper-trading state: cash account + open positions."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from backend.db import Base


class PaperAccount(Base):
    """Single-row table holding cash and a snapshot of last portfolio value."""

    __tablename__ = "paper_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    starting_cash: Mapped[float] = mapped_column(Float, default=1000.0)
    cash: Mapped[float] = mapped_column(Float, default=1000.0)
    last_portfolio_value: Mapped[float] = mapped_column(Float, default=1000.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self) -> dict:
        return {
            "starting_cash": self.starting_cash,
            "cash": self.cash,
            "last_portfolio_value": self.last_portfolio_value,
            "realized_pnl": self.realized_pnl,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class PaperPosition(Base):
    """Open paper position. Options carry their metadata as JSON in ``meta``.

    P2.2 — stored greeks + entry IV columns. Set when the position is
    opened from a real ThetaData chain quote (or BS fallback). These
    let the MTM repricer (P2.3) compute a fresh price even when the
    chain is unavailable mid-cycle.
    """

    __tablename__ = "paper_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String, default="stock")  # stock | option | complex
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    meta: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)

    # P2.2 — option-only fields (NULL for stock). Captured at entry from
    # the live chain or BS fallback so MTM can reprice consistently.
    strike: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expiration: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    option_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # call|put
    entry_bid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_ask: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_mid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_iv: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_delta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_gamma: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_theta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_vega: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    entry_underlying: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # P1.5 — provenance: which data source priced this entry.
    pricing_source: Mapped[str] = mapped_column(String, default="paper_stub")
    # P2.4 — last-known fresh IV (refreshed when chain is fresh).
    stored_iv: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stored_iv_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # EXIT.1 — peak-premium tracking for adaptive trailing exit. We track
    # the high-water mark of the per-share premium since entry so the exit
    # manager can compute drawdown-from-peak. Once a position passes the
    # monitor threshold (default +15% gain), the trailing logic engages.
    peak_premium_per_share: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, default=None,
    )
    peak_premium_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None,
    )
    # Track IV separately from stored_iv so we can detect IV crush
    # (current IV << entry IV) regardless of when stored_iv was last
    # refreshed. peak_premium_at also tells us how stale "peak" is.
    last_iv_seen: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True, default=None,
    )
    last_iv_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, default=None,
    )

    # MITS Phase 17.A — observability of chain freshness, IV crush,
    # MTM cadence, and the "options paused" runtime flag.
    chain_freshness_at_entry_sec:  Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    iv_crush_first_detected_at:    Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_marked_at:                Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    options_disabled:              Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "kind": self.kind,
            "quantity": self.quantity,
            "avg_cost": self.avg_cost,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "meta": json.loads(self.meta) if self.meta else None,
            "strike": self.strike,
            "expiration": self.expiration,
            "option_type": self.option_type,
            "entry_bid": self.entry_bid,
            "entry_ask": self.entry_ask,
            "entry_mid": self.entry_mid,
            "entry_iv": self.entry_iv,
            "entry_delta": self.entry_delta,
            "entry_gamma": self.entry_gamma,
            "entry_theta": self.entry_theta,
            "entry_vega": self.entry_vega,
            "entry_underlying": self.entry_underlying,
            "pricing_source": self.pricing_source,
            "stored_iv": self.stored_iv,
            "stored_iv_at": (self.stored_iv_at.isoformat()
                                  if self.stored_iv_at else None),
            "peak_premium_per_share": self.peak_premium_per_share,
            "peak_premium_at": (self.peak_premium_at.isoformat()
                                if self.peak_premium_at else None),
            "last_iv_seen": self.last_iv_seen,
            "last_iv_seen_at": (self.last_iv_seen_at.isoformat()
                                if self.last_iv_seen_at else None),
            "chain_freshness_at_entry_sec": self.chain_freshness_at_entry_sec,
            "iv_crush_first_detected_at": (
                self.iv_crush_first_detected_at.isoformat()
                if self.iv_crush_first_detected_at else None
            ),
            "last_marked_at": (
                self.last_marked_at.isoformat()
                if self.last_marked_at else None
            ),
            "options_disabled": bool(self.options_disabled),
        }


def get_or_create_account(session: Session, starting_cash: float = 1000.0) -> PaperAccount:
    account = session.get(PaperAccount, 1)
    if account is None:
        account = PaperAccount(
            id=1,
            starting_cash=starting_cash,
            cash=starting_cash,
            last_portfolio_value=starting_cash,
        )
        session.add(account)
        session.flush()
    return account
