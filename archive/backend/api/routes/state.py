"""Stage-12.A3 MarketState endpoints.

  • ``GET /state/current``  — latest MarketState the engine built (or null)
  • ``POST /state/preview`` — build MarketState from a hypothetical context
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.bot.state import build_market_state, get_latest

router = APIRouter(prefix="/state", tags=["state"])


@router.get("/current")
async def current() -> dict:
    state = get_latest()
    if state is None:
        return {"state": None,
                "reason": "no engine cycle has built a market state yet"}
    return {"state": state.to_dict()}


class PreviewBody(BaseModel):
    snapshot: Optional[Dict[str, Any]] = None
    regime: Optional[Dict[str, Any]] = None
    cross_asset: Optional[Dict[str, Any]] = None
    features: Optional[Dict[str, Any]] = None
    event_risk: Optional[Dict[str, Any]] = None


@router.post("/preview")
async def preview(body: PreviewBody) -> dict:
    return {"state": build_market_state(**body.model_dump()).to_dict()}
