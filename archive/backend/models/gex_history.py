"""Heatseeker: periodic GEX regime snapshots for the history endpoint."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class GexRegimeHistory(Base):
    __tablename__ = "gex_regime_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    spot_price: Mapped[float] = mapped_column(Float, default=0.0)
    call_wall: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    put_wall: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gamma_flip: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dealer_regime: Mapped[str] = mapped_column(String, default="unknown")
    # MITS Phase 2 — scalar net-GEX proxy so corpus GEXAcceleration detector
    # can fire on historical data. Sign comes from dealer_regime
    # (long_gamma → positive, short_gamma → negative). Magnitude is a
    # distance-to-flip proxy: |spot - gamma_flip| * 1e9 — coarse but
    # monotonic with the actual dealer hedging need. When a real net-GEX
    # number arrives from a Pro-tier vendor, this column just gets written
    # directly. Documented formula lives in `_compute_net_gex_scalar`
    # (see `backend/bot/data/options.py`) and the backfill migration.
    net_gex_scalar: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "spot_price": self.spot_price,
            "call_wall": self.call_wall,
            "put_wall": self.put_wall,
            "gamma_flip": self.gamma_flip,
            "dealer_regime": self.dealer_regime,
            "net_gex_scalar": self.net_gex_scalar,
        }
