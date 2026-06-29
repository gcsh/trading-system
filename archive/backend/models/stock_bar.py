"""MITS Phase 11.B.1 — Silver-layer typed stock bar row.

Used by the ThetaData stock backfill to land normalized rows in SQLite
(small in-process queries) alongside the bronze parquet (raw payload
for replay / re-derivation). The ``interval`` column distinguishes
daily / minute / 5m / 15m / 60m bars in a single table so the API can
ask the gold layer for "AAPL 1d, last 5 years" or "SPY 1m, today" with
the same shape.

External-cache-shaped — survives a paper reset. The backfill alone
costs hours; replaying after every reset would be absurd.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class StockBar(Base):
    __tablename__ = "stock_bars"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "interval", "bar_ts",
            name="uq_stock_bars_ticker_interval_ts",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    # "1d", "1m", "5m", "15m", "60m" — matches ThetaData v3 ``interval`` param
    # for intraday; "1d" is the EOD endpoint.
    interval: Mapped[str] = mapped_column(String, index=True)
    # ET-local bar open timestamp. For "1d" this is midnight of the bar's date.
    bar_ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    open: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    high: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    low: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    close: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    volume: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vwap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trades: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String, default="thetadata")
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "interval": self.interval,
            "bar_ts": self.bar_ts.isoformat() if self.bar_ts else None,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "vwap": self.vwap,
            "trades": self.trades,
            "source": self.source,
            "fetched_at": (self.fetched_at.isoformat()
                            if self.fetched_at else None),
        }
