"""MITS Phase 11.B.2 — Silver-layer EOD option contract bar.

One row per (ticker, expiration, strike, right, bar_date). Populated by
the ThetaData options history backfill (per-contract endpoint, since
the operator's Standard tier doesn't include the bulk_history shape).

This is THE corpus that lets the options-replay layer compute IV
percentile per DTE bucket, GEX-by-strike walls, dealer regime, and
historical analog matches for the Opportunity Brain. ~20M rows
expected across 40 tickers × 5y × ~30 strikes × 2 rights × ~250
expirations.

External-cache-shaped — survives :func:`fresh_start`. Pulling 5y of
option chains again would burn a full day per reset; listed in
``EXTERNAL_CACHE_TABLES`` in ``backend/bot/system_reset.py``.

PK invariant
============

INSERT OR IGNORE on (ticker, expiration, strike, right, bar_date) is
the dedupe contract that lets a chunk be safely re-run. The
SyncOrchestrator's resume path relies on this being a no-op when a
chunk's rows are already in the table.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (Date, DateTime, Float, Index, Integer, String,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class OptionContractBar(Base):
    __tablename__ = "option_contract_bars"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "expiration", "strike", "right", "bar_date",
            name="uq_option_contract_bars_full",
        ),
        # Cohort queries: "all rows for AAPL on 2021-06-09".
        Index("ix_ocb_ticker_date", "ticker", "bar_date"),
        # "All rows for AAPL's 2026-06-20 expiry across all dates".
        Index("ix_ocb_ticker_expiry", "ticker", "expiration"),
        # IV-percentile cohort cross-section ("which contracts had >80
        # IV percentile on this date") — used by options_replay.
        Index("ix_ocb_date_iv", "bar_date", "iv"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    expiration: Mapped[date] = mapped_column(Date, index=True)
    # Strike in DOLLARS (e.g. 130.00). NOT strike × 1000 — ThetaData v3
    # returns decimal dollars natively, we don't rescale on insert.
    strike: Mapped[float] = mapped_column(Float)
    # "C" or "P" — uppercase single char so cohort queries stay cheap.
    right: Mapped[str] = mapped_column(String(1))
    bar_date: Mapped[date] = mapped_column(Date, index=True)

    open: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ask: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Mid-price = (bid + ask) / 2 when both present, else close. Cached
    # so cohort queries don't have to re-compute it row-by-row.
    mid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # IV + greeks — populated lazily by options_replay (the EOD chain
    # endpoint doesn't return them on Standard tier). Kept on the same
    # row so a single SELECT delivers a complete contract snapshot.
    iv: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    delta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gamma: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vega: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    theta: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    open_interest: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Number of trades that day (ThetaData's ``count`` field).
    trade_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String, default="thetadata")
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "expiration": self.expiration.isoformat() if self.expiration else None,
            "strike": self.strike,
            "right": self.right,
            "bar_date": self.bar_date.isoformat() if self.bar_date else None,
            "open": self.open, "high": self.high, "low": self.low,
            "close": self.close, "bid": self.bid, "ask": self.ask,
            "mid": self.mid, "iv": self.iv,
            "delta": self.delta, "gamma": self.gamma,
            "vega": self.vega, "theta": self.theta,
            "volume": self.volume, "open_interest": self.open_interest,
            "trade_count": self.trade_count,
            "source": self.source,
            "fetched_at": (self.fetched_at.isoformat()
                           if self.fetched_at else None),
        }


__all__ = ["OptionContractBar"]
