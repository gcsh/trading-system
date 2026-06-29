"""Stage-13.D10 Decision Marketplace endpoints.

  • ``POST /marketplace/preview`` — score + select a hypothetical candidate set
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.bot.marketplace import (
    Candidate,
    candidate_from,
    select,
)

router = APIRouter(prefix="/marketplace", tags=["marketplace"])


class CandidateBody(BaseModel):
    ticker: str
    action: str = "BUY_STOCK"
    strategy: str = ""
    stop_pct: Optional[float] = None       # percentage points (e.g. 5.0)
    take_profit_pct: Optional[float] = None
    probability: Optional[float] = None    # 0-1
    capital_required: float = 1000.0
    liquidity_score: float = 1.0
    confidence: float = 0.5
    metadata: Optional[Dict[str, Any]] = None


class PreviewBody(BaseModel):
    candidates: List[CandidateBody]
    capital_available: float = 10000.0
    max_positions: int = 10
    min_expected_value: float = 0.0
    min_score_per_dollar: float = 0.0


@router.post("/preview")
async def preview(body: PreviewBody) -> dict:
    candidates: List[Candidate] = []
    for c in body.candidates:
        candidates.append(candidate_from(
            ticker=c.ticker, action=c.action, strategy=c.strategy,
            stop_pct=c.stop_pct, take_profit_pct=c.take_profit_pct,
            probability=c.probability,
            capital_required=c.capital_required,
            liquidity_score=c.liquidity_score,
            confidence=c.confidence,
            metadata=c.metadata or {},
        ))
    result = select(
        candidates,
        capital_available=body.capital_available,
        max_positions=body.max_positions,
        min_expected_value=body.min_expected_value,
        min_score_per_dollar=body.min_score_per_dollar,
    )
    return result.to_dict()
