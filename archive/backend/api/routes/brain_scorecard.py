"""MITS Phase 14.D — Brain scorecard + recent predictions endpoints."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query
from sqlalchemy import desc, select

from backend.bot.scorecard.brain_scorecard import build_brain_scorecard
from backend.db import session_scope
from backend.models.brain_prediction import BrainPrediction

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/brain", tags=["brain"])


@router.get("/scorecard")
async def get_brain_scorecard(
    window: int = Query(50, ge=10, le=500),
    surface: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Brain calibration scorecard + the recent predictions backing it."""
    card = build_brain_scorecard(surface=surface, window_trades=window)
    with session_scope() as s:
        q = (
            select(BrainPrediction)
            .order_by(desc(BrainPrediction.created_at))
            .limit(int(window))
        )
        if surface:
            q = q.where(BrainPrediction.surface == surface)
        rows = s.execute(q).scalars().all()
        recent = [r.to_dict() for r in rows]
    return {
        "surface": surface,
        "window": int(window),
        "scorecard": card.to_dict(),
        "recent_predictions": recent,
    }


@router.get("/predictions")
async def list_predictions(
    limit: int = Query(50, ge=1, le=500),
    surface: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
) -> Dict[str, Any]:
    with session_scope() as s:
        q = (
            select(BrainPrediction)
            .order_by(desc(BrainPrediction.created_at))
            .limit(int(limit))
        )
        if surface:
            q = q.where(BrainPrediction.surface == surface)
        if outcome:
            q = q.where(BrainPrediction.outcome == outcome)
        rows = s.execute(q).scalars().all()
        items = [r.to_dict() for r in rows]
    return {
        "count": len(items),
        "predictions": items,
    }
