"""Stage-18a — SEC EDGAR filings cache.

One row per filing. We pull the recent-filings list per CIK and dedupe
on (cik, accession_number). Only metadata is stored — the actual
filing document stays at SEC servers and can be linked via the
SEC viewer URL derived from accession_number.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class EdgarFiling(Base):
    __tablename__ = "edgar_filings"
    __table_args__ = (UniqueConstraint("cik", "accession_number",
                                            name="uq_edgar_cik_accession"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cik: Mapped[str] = mapped_column(String, index=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    accession_number: Mapped[str] = mapped_column(String)
    form: Mapped[str] = mapped_column(String, index=True)   # 8-K, 10-Q, 10-K, 4, etc.
    filed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    primary_document: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    items: Mapped[Optional[str]] = mapped_column(String, nullable=True)   # 8-K item codes
    is_insider_buy: Mapped[bool] = mapped_column(default=False)
    is_insider_sell: Mapped[bool] = mapped_column(default=False)
    reporter: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "cik": self.cik, "ticker": self.ticker,
            "accession_number": self.accession_number,
            "form": self.form,
            "filed_at": self.filed_at.isoformat() if self.filed_at else None,
            "primary_document": self.primary_document,
            "items": self.items,
            "is_insider_buy": self.is_insider_buy,
            "is_insider_sell": self.is_insider_sell,
            "reporter": self.reporter,
            "viewer_url": (f"https://www.sec.gov/cgi-bin/browse-edgar"
                              f"?action=getcompany&CIK={self.cik}"
                              if self.cik else None),
        }
