"""MITS Phase 2 — intraday IV cache.

ThetaData Standard tier does not expose a historical intraday IV/GEX
endpoint (those are Pro-tier). The Phase 2 workaround samples the
straddle quote at each historical bar timestamp via the
historical-chain-quote endpoint (which Standard DOES expose) and inverts
to ATM IV via Brenner-Subrahmanyam:

    IV = straddle / (k * S * sqrt(T)),  k = sqrt(2*pi) / 2 ≈ 1.2533

Each (ticker, timestamp) result is cached in this table so re-runs of
the historical replay don't hammer ThetaData with the same quote
request again. Historical data is immutable, so cache TTL is infinite.

Listed in EXTERNAL_CACHE_TABLES — derived from public market data, not
bot decisions, so preserved across fresh_start.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class IntradayIVCache(Base):
    __tablename__ = "intraday_iv_cache"
    __table_args__ = (
        UniqueConstraint("ticker", "timestamp",
                         name="uq_intraday_iv_cache_ticker_ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    iv_atm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Pricing inputs preserved so we can audit / re-derive if formulas change.
    straddle: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    spot: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    strike: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expiry: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    dte: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # ``ok`` | ``no_quote`` | ``stale`` | ``oob_iv`` | ``error`` — non-ok
    # rows are cached so a known-failed timestamp doesn't get retried on
    # every replay.
    status: Mapped[str] = mapped_column(String, default="ok")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "iv_atm": self.iv_atm,
            "straddle": self.straddle,
            "spot": self.spot,
            "strike": self.strike,
            "expiry": self.expiry,
            "dte": self.dte,
            "status": self.status,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }
