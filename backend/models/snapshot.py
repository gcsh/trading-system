"""Portfolio equity snapshots — one row per cycle for the equity curve."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class PortfolioSnapshot(Base):
    """Point-in-time equity reading. Engine writes one per ``run_cycle``.

    P1.6 — quality fields make the equity curve auditable forever:
      * ``data_quality`` — good | partial | degraded
      * ``accounting_version`` — pricing-model generation that produced
        the equity number (v1 = stubbed math, v2 = real chain pricing)
      * ``pricing_source_mix`` — JSON breakdown of how positions were
        marked (e.g. ``{"thetadata": 0.8, "bs_fallback": 0.2}``)
      * ``excludes_synthetic`` — declares whether equity was derived
        from live-only or live+synthetic trades. Set on every write so
        future analytics can prove what the snapshot measured.
    """

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    portfolio_value: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    broker: Mapped[str] = mapped_column(String, default="local_paper")

    # P1.6 — quality / provenance fields.
    data_quality: Mapped[str] = mapped_column(String, default="good")
    accounting_version: Mapped[int] = mapped_column(Integer, default=1)
    pricing_source_mix: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    excludes_synthetic: Mapped[int] = mapped_column(Integer, default=1)

    def to_dict(self) -> dict:
        import json as _json
        try:
            mix = _json.loads(self.pricing_source_mix) if self.pricing_source_mix else None
        except Exception:
            mix = None
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "portfolio_value": self.portfolio_value,
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "open_positions": self.open_positions,
            "broker": self.broker,
            "data_quality": self.data_quality,
            "accounting_version": self.accounting_version,
            "pricing_source_mix": mix,
            "excludes_synthetic": bool(self.excludes_synthetic),
        }
