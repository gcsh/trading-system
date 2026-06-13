"""MITS Phase 0 — Market Observation (raw pattern detection event).

A `MarketObservation` is the atomic output of a Detector: "pattern X
fired on ticker Y at timestamp T, with these features". Stored verbatim
so downstream consumers (the outcome linker, the knowledge aggregator,
the AI Brain memory layer) can replay or re-aggregate without re-running
detectors against historical bars.

Indexed on (ticker, pattern, timestamp) so per-ticker / per-pattern
queries are cheap. `features` is a JSON string blob — keep it lean
(<2KB) so the table doesn't explode at corpus scale.

This is *external-cache-shaped* data: it survives `fresh_start` because
it's derived from public market history, not bot decisions.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, Index, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class MarketObservation(Base):
    __tablename__ = "market_observations"
    __table_args__ = (
        # A given pattern fires at most once per (ticker, timestamp,
        # timeframe). Idempotent re-runs of bootstrap_ticker should
        # skip duplicates via this constraint.
        UniqueConstraint(
            "ticker", "pattern", "timestamp", "timeframe",
            name="uq_market_obs_ticker_pattern_ts_tf",
        ),
        Index("ix_market_obs_ticker_pattern", "ticker", "pattern"),
        # MITS Phase 12.1 — direction-aware outcome scoring.
        # Indexed alongside pattern so the family-edge endpoint can
        # aggregate `WHERE direction = 'short'` in one scan.
        Index("ix_market_obs_pattern_direction", "pattern", "direction"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String, index=True)
    pattern: Mapped[str] = mapped_column(String, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)
    # "1d", "1h", "5m", etc. Keeps daily-bar observations distinct from
    # intraday ones even when they fall on the same calendar day.
    timeframe: Mapped[str] = mapped_column(String, default="1d")

    # Categorical context as of the observation timestamp. Used as
    # cohort axes in the knowledge graph.
    regime: Mapped[str] = mapped_column(String, default="unknown")
    vol_state: Mapped[str] = mapped_column(String, default="normal")
    time_bucket: Mapped[str] = mapped_column(String, default="rth")

    # Spot price at the observation bar's close. Stored so outcome
    # linker can compute forward returns without re-fetching bars.
    spot: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    iv_rank: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gex_state: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Per-pattern features as a JSON string. Schema is detector-defined.
    features: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Where this row came from — "historical_replay" | "live_engine" |
    # "manual". Lets downstream consumers filter by provenance.
    source: Mapped[str] = mapped_column(String, default="historical_replay",
                                              index=True)

    # MITS Phase 12.1 — directional tag for outcome scoring.
    # Values:
    #   'long'    — bullish setup, return > 0 = winner
    #   'short'   — bearish setup, return < 0 = winner (inverted)
    #   'neutral' — volatility/event signal, |return| > threshold = winner
    #   None      — legacy / unknown direction (falls back to long bias)
    # Backfilled from the authoritative pattern mapping; new detectors
    # set this via Observation.direction in their emit path.
    direction: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, default="long", index=True)

    # MITS Phase 11.J — cross-vendor parity flag. Set True by the parity
    # audit pass when the observation's (ticker, timestamp.date()) pair
    # has a `parity_audit_history` row with severity != "ok". Downstream
    # consumers (knowledge_aggregator, AI Brain context) can demote these
    # observations rather than count them at full weight. Default False
    # — the parity audit promotes rows AFTER they land, not at insert.
    parity_warn: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        feats: Optional[dict] = None
        if self.features:
            try:
                feats = json.loads(self.features)
            except Exception:
                feats = None
        return {
            "id": self.id,
            "ticker": self.ticker,
            "pattern": self.pattern,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "timeframe": self.timeframe,
            "regime": self.regime,
            "vol_state": self.vol_state,
            "time_bucket": self.time_bucket,
            "spot": self.spot,
            "iv_rank": self.iv_rank,
            "gex_state": self.gex_state,
            "features": feats,
            "source": self.source,
            "direction": self.direction,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
