"""MITS Phase 11.E — 13F-HR per-position row.

One row per (fund_cik, quarter_end_date, ticker_or_cusip). The
``change_from_prior_qtr`` field is computed at write time by diffing
against the prior quarter's row for the same fund + ticker.

External-cache-shaped — survives a paper reset.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Date, DateTime, Float, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class FundHolding(Base):
    __tablename__ = "fund_holdings"
    __table_args__ = (
        UniqueConstraint(
            "fund_cik", "quarter_end_date", "cusip",
            name="uq_fund_holdings_fund_quarter_cusip",
        ),
        Index("ix_fund_holdings_ticker_quarter", "ticker",
              "quarter_end_date"),
        Index("ix_fund_holdings_fund_quarter", "fund_cik",
              "quarter_end_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fund_cik: Mapped[str] = mapped_column(String, index=True)
    fund_name: Mapped[str] = mapped_column(String, index=True)
    # Resolved ticker (may be NULL when the CUSIP doesn't map to a
    # known equity — non-equity positions or thinly-traded names).
    ticker: Mapped[Optional[str]] = mapped_column(
        String, index=True, nullable=True)
    cusip: Mapped[str] = mapped_column(String, index=True)
    issuer_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # 13F report period end (e.g. 2024-12-31 for Q4 2024).
    quarter_end_date: Mapped[date] = mapped_column(Date, index=True)
    shares: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    value_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pct_of_portfolio: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)
    # Signed delta of share count vs the same fund's prior 13F quarter.
    # Positive = adding; negative = trimming; NULL = first quarter we
    # have on file for this (fund, ticker).
    change_from_prior_qtr: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True)
    filing_date: Mapped[Optional[date]] = mapped_column(
        Date, index=True, nullable=True)
    accession_number: Mapped[Optional[str]] = mapped_column(
        String, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "fund_cik": self.fund_cik,
            "fund_name": self.fund_name,
            "ticker": self.ticker,
            "cusip": self.cusip,
            "issuer_name": self.issuer_name,
            "quarter_end_date": (self.quarter_end_date.isoformat()
                                   if self.quarter_end_date else None),
            "shares": self.shares,
            "value_usd": self.value_usd,
            "pct_of_portfolio": self.pct_of_portfolio,
            "change_from_prior_qtr": self.change_from_prior_qtr,
            "filing_date": (self.filing_date.isoformat()
                              if self.filing_date else None),
            "accession_number": self.accession_number,
            "source_url": self.source_url,
            "fetched_at": (self.fetched_at.isoformat()
                            if self.fetched_at else None),
        }
