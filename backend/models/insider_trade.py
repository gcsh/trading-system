"""MITS Phase 11.E — Form 4 insider transaction row.

One row per ``(cik, accession_number, transaction_code, transaction_date,
shares, price)`` — i.e. per Form 4 *transaction* line, not per filing.
A single Form 4 can disclose multiple lines (cluster of P/S/M codes),
so we parse the XBRL/XML primary doc and emit one row per
``<nonDerivativeTransaction>`` block.

External-cache-shaped — survives a paper reset.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Boolean, Date, DateTime, Float, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class InsiderTrade(Base):
    __tablename__ = "insider_trades"
    __table_args__ = (
        # Within a single Form 4 the (transaction_date, code, shares, price)
        # tuple uniquely identifies a line — the same insider can hit the
        # same code+price twice on the same day. Including ``insider_name``
        # disambiguates Form 4s that report multiple reporters.
        UniqueConstraint(
            "cik", "accession_number", "insider_name",
            "transaction_code", "transaction_date", "shares", "price",
            name="uq_insider_trades_row",
        ),
        Index("ix_insider_trades_ticker_txn", "ticker", "transaction_date"),
        Index("ix_insider_trades_cik_filing", "cik", "filing_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    cik: Mapped[str] = mapped_column(String, index=True)
    accession_number: Mapped[str] = mapped_column(String, index=True)
    filing_date: Mapped[date] = mapped_column(Date, index=True)
    transaction_date: Mapped[date] = mapped_column(Date, index=True)
    insider_name: Mapped[str] = mapped_column(String, index=True)
    insider_role: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Form 4 transaction codes: P=purchase, S=sale, M=exercise, A=grant,
    # F=tax-withhold, G=gift, etc. Stored as-is; the feature layer
    # decides which codes count as "buy" vs "sell".
    transaction_code: Mapped[str] = mapped_column(String, index=True)
    shares: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_director: Mapped[bool] = mapped_column(Boolean, default=False)
    is_officer: Mapped[bool] = mapped_column(Boolean, default=False)
    is_10pct_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    source_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "cik": self.cik,
            "accession_number": self.accession_number,
            "filing_date": (self.filing_date.isoformat()
                              if self.filing_date else None),
            "transaction_date": (self.transaction_date.isoformat()
                                   if self.transaction_date else None),
            "insider_name": self.insider_name,
            "insider_role": self.insider_role,
            "transaction_code": self.transaction_code,
            "shares": self.shares,
            "price": self.price,
            "total_value": self.total_value,
            "is_director": bool(self.is_director),
            "is_officer": bool(self.is_officer),
            "is_10pct_owner": bool(self.is_10pct_owner),
            "source_url": self.source_url,
            "fetched_at": (self.fetched_at.isoformat()
                            if self.fetched_at else None),
        }
