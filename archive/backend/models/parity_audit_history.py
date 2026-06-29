"""MITS Phase 11.J — cross-vendor parity audit row.

One row per (ticker, date, source_a, source_b) triple. Records the
divergence between two vendors' closing prices on the same calendar day
so we can attribute later corpus losses to "stale yfinance data" vs
"agent logic missed it".

The audit is asymmetric in name but symmetric in value: source_a is the
LEGACY pull (typically yfinance, frozen pre-Phase 11) and source_b is
the NEW canonical pull (typically ThetaData EOD). ``divergence_pct`` =
|close_a - close_b| / close_b — clipped to [0, 1] for sanity.

External-cache-shaped — derived from public market data, survives a
paper reset. Listed in ``EXTERNAL_CACHE_TABLES``.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Date, DateTime, Float, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class ParityAuditHistory(Base):
    __tablename__ = "parity_audit_history"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "audit_date", "source_a", "source_b",
            name="uq_parity_audit_row",
        ),
        Index("ix_parity_audit_ticker_date", "ticker", "audit_date"),
        Index("ix_parity_audit_severity", "severity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    audit_date: Mapped[date] = mapped_column(Date, index=True)
    source_a: Mapped[str] = mapped_column(String, index=True)
    source_b: Mapped[str] = mapped_column(String, index=True)
    close_a: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    close_b: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # |close_a - close_b| / close_b, clamped [0, 1] for sanity. NULL when
    # either close is missing or close_b is zero/negative.
    divergence_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # "ok" (<0.5%) | "warn" (0.5%-2%) | "suspect" (>=2%) | "missing"
    severity: Mapped[str] = mapped_column(String, default="ok", index=True)
    audited_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ticker": self.ticker,
            "audit_date": (self.audit_date.isoformat()
                            if self.audit_date else None),
            "source_a": self.source_a,
            "source_b": self.source_b,
            "close_a": self.close_a,
            "close_b": self.close_b,
            "divergence_pct": self.divergence_pct,
            "severity": self.severity,
            "audited_at": (self.audited_at.isoformat()
                            if self.audited_at else None),
        }
