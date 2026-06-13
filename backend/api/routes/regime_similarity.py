"""Stage-13.C5 Regime Similarity endpoints.

  • ``POST /regimes/similar``       — find K most-similar past regimes for a target
  • ``GET  /regimes/similar/current`` — convenience for the live MarketState
  • ``POST /regimes/snapshot``      — manually capture a snapshot (e.g. from a cron)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from backend.bot.regime_similarity import (
    aggregate_outcomes,
    find_similar,
    snapshot_current,
)
from backend.bot.state import get_latest

router = APIRouter(prefix="/regimes", tags=["regime_similarity"])


class SimilarBody(BaseModel):
    target: Dict[str, Any]
    k: int = 20
    min_similarity: float = 0.50


@router.post("/similar")
async def similar(body: SimilarBody) -> dict:
    matches = find_similar(body.target, k=body.k,
                              min_similarity=body.min_similarity)
    return {
        "matches": [m.to_dict() for m in matches],
        "summary": aggregate_outcomes(matches),
    }


@router.get("/similar/current")
async def similar_current(k: int = Query(20, ge=1, le=100)) -> dict:
    state = get_latest()
    if state is None:
        return {"matches": [], "summary": aggregate_outcomes([]),
                "reason": "no engine cycle has built a market state yet"}
    matches = find_similar(state.to_dict(), k=k)
    return {
        "matches": [m.to_dict() for m in matches],
        "summary": aggregate_outcomes(matches),
    }


class SnapshotBody(BaseModel):
    breadth_score: float = 0.0
    sentiment_score: float = 0.0
    sector_strength: float = 0.0
    rates_10y: Optional[float] = None
    dollar_dxy: Optional[float] = None


@router.post("/snapshot")
async def snapshot(body: SnapshotBody) -> dict:
    state = get_latest()
    if state is None:
        return {"snapshot_id": None,
                "reason": "no market state available — start the engine first"}
    sid = snapshot_current(state, **body.model_dump())
    return {"snapshot_id": sid}
