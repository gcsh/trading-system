"""Per-order execution telemetry — expected vs filled price, slippage in bps,
linked to the underlying Trade. Powers /execution/insights and the dashboard's
execution-quality card."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class ExecutionLog(Base):
    __tablename__ = "execution_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    side: Mapped[str] = mapped_column(String)                 # BUY | SELL
    quantity: Mapped[float] = mapped_column(Float)
    expected_price: Mapped[float] = mapped_column(Float)      # snapshot at signal time
    fill_price: Mapped[float] = mapped_column(Float)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)        # signed $, positive = adverse
    slippage_bps: Mapped[float] = mapped_column(Float, default=0.0)    # signed, positive = adverse
    is_adverse: Mapped[int] = mapped_column(Integer, default=0)
    trade_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "ticker": self.ticker, "side": self.side, "quantity": self.quantity,
            "expected_price": self.expected_price, "fill_price": self.fill_price,
            "slippage": self.slippage, "slippage_bps": self.slippage_bps,
            "is_adverse": bool(self.is_adverse), "trade_id": self.trade_id,
        }
