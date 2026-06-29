"""MITS-5 — thesis-health endpoint.

GET /thesis/health/{position_id}
    Returns the current thesis-health score + intact/degraded trait
    breakdown for one open paper position. Used by
    `CurrentlyHoldingStrip` to render the per-position health chip +
    drill-down modal.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException

from backend.bot.thesis import (
    build_winner_profile,
    calculate_health,
)
from backend.db import session_scope
from backend.models.paper import PaperPosition

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/thesis", tags=["thesis"])


@router.get("/health/{position_id}")
async def position_health(position_id: int) -> dict:
    """Compute the live thesis-health score for one open position."""
    with session_scope() as session:
        row = session.query(PaperPosition).filter(
            PaperPosition.id == int(position_id)
        ).first()
        if row is None:
            raise HTTPException(status_code=404,
                                       detail=f"position {position_id} not found")
        pos_dict = row.to_dict()

    meta = pos_dict.get("meta") or {}
    pattern = (meta.get("pattern")
                  or meta.get("detector_pattern")
                  or "")
    regime = meta.get("regime") or ""
    ticker = pos_dict.get("ticker") or ""

    if not pattern:
        return {
            "position_id": position_id,
            "ticker": ticker,
            "score": None,
            "abstain": True,
            "reason": "no detector pattern recorded on entry — abstain",
            "intact_traits": [],
            "degraded_traits": [],
        }

    profile = build_winner_profile(
        pattern=pattern, regime=regime, horizon="1d", ticker=ticker,
    )

    # Hydrate the position with current market data for trait checks.
    pos_for_health = dict(pos_dict)
    pos_for_health.setdefault("current_price", pos_dict.get("stored_iv"))
    try:
        from backend.bot.market_data import MarketDataAdapter
        snap = MarketDataAdapter().snapshot(ticker)
        if snap and snap.data:
            pos_for_health.setdefault("vwap", snap.data.get("vwap"))
    except Exception:
        pass

    health = calculate_health(pos_for_health, None, profile)
    return {
        "position_id": position_id,
        "ticker": ticker,
        "pattern": pattern,
        "regime": regime,
        "winner_profile": profile.to_dict(),
        **health.to_dict(),
    }
