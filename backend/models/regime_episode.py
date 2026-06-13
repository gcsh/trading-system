"""Stage-13.C5 RegimeEpisodeSnapshot — periodic full-system regime fingerprint.

One row per snapshot (engine cycle, or periodic job) capturing the
cross-asset regime fingerprint: VIX, rates, dollar, breadth, sentiment,
sector strength, gamma. Separate from individual trades — this is the
*macro fingerprint* of the moment, not a per-trade decision.

Used by ``bot/memory/similar_regimes`` to answer:
  "What were the 20 historical regimes most similar to right now,
   and which strategies worked best in them?"
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class RegimeEpisodeSnapshot(Base):
    __tablename__ = "regime_episode_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime,
                                                  default=datetime.utcnow, index=True)

    # Categorical axes (from MarketState)
    trend: Mapped[str] = mapped_column(String, default="unknown", index=True)
    trend_phase: Mapped[str] = mapped_column(String, default="neutral")
    volatility: Mapped[str] = mapped_column(String, default="normal")
    vol_phase: Mapped[str] = mapped_column(String, default="neutral")
    gamma: Mapped[str] = mapped_column(String, default="unknown")
    risk: Mapped[str] = mapped_column(String, default="neutral")
    equities: Mapped[str] = mapped_column(String, default="unknown")
    yields: Mapped[str] = mapped_column(String, default="unknown")
    dollar: Mapped[str] = mapped_column(String, default="unknown")
    label: Mapped[str] = mapped_column(String, default="")

    # Numeric axes — needed for similarity distance
    vix: Mapped[float] = mapped_column(Float, default=0.0)
    iv_rank: Mapped[float] = mapped_column(Float, default=0.0)
    breadth_score: Mapped[float] = mapped_column(Float, default=0.0)
    sentiment_score: Mapped[float] = mapped_column(Float, default=0.0)
    sector_strength: Mapped[float] = mapped_column(Float, default=0.0)
    rates_10y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    dollar_dxy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Optional forward outcome — backfilled by a scheduled job. Lets the
    # similarity engine answer "in similar regimes, what was the forward
    # win rate / mean return?".
    fwd_1d_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fwd_5d_return: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fwd_trades_count: Mapped[int] = mapped_column(Integer, default=0)
    fwd_trades_wins: Mapped[int] = mapped_column(Integer, default=0)
    fwd_trades_pnl: Mapped[float] = mapped_column(Float, default=0.0)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "trend": self.trend, "trend_phase": self.trend_phase,
            "volatility": self.volatility, "vol_phase": self.vol_phase,
            "gamma": self.gamma, "risk": self.risk,
            "equities": self.equities, "yields": self.yields,
            "dollar": self.dollar, "label": self.label,
            "vix": self.vix, "iv_rank": self.iv_rank,
            "breadth_score": self.breadth_score,
            "sentiment_score": self.sentiment_score,
            "sector_strength": self.sector_strength,
            "rates_10y": self.rates_10y, "dollar_dxy": self.dollar_dxy,
            "fwd_1d_return": self.fwd_1d_return,
            "fwd_5d_return": self.fwd_5d_return,
            "fwd_trades_count": self.fwd_trades_count,
            "fwd_trades_wins": self.fwd_trades_wins,
            "fwd_trades_pnl": self.fwd_trades_pnl,
        }
