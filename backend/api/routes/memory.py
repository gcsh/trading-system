"""Stage-11.5 Memory Layer endpoints.

  • ``GET  /memory/episodes``               — list recent regime episodes
  • ``POST /memory/recall``                 — recall K most-similar past trades
  • ``GET  /memory/recall/trade/{id}``      — recall for an existing trade
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.bot.memory import (
    build_episodes,
    recall_similar,
    recall_summary,
)
from backend.db import session_scope
from backend.models.trade import Trade

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("/episodes")
async def episodes(limit: int = Query(2000, ge=50, le=5000)) -> dict:
    eps = build_episodes(limit=limit)
    return {"episodes": [e.to_dict() for e in eps], "count": len(eps)}


class RecallBody(BaseModel):
    ticker: Optional[str] = None
    action: Optional[str] = None
    analytics: Optional[Dict[str, Any]] = None
    features: Optional[Dict[str, Any]] = None
    regime: Optional[Dict[str, Any]] = None
    k: int = 5
    min_similarity: float = 0.45


@router.post("/recall")
async def recall(body: RecallBody) -> dict:
    matches = recall_similar(body.model_dump(exclude={"k", "min_similarity"}),
                                k=body.k, min_similarity=body.min_similarity)
    return {
        "matches": [m.to_dict() for m in matches],
        "summary": recall_summary(matches),
    }


@router.get("/recall/trade/{trade_id}")
async def recall_for_trade(trade_id: int,
                              k: int = Query(5, ge=1, le=25)) -> dict:
    """Find historical analogues for an existing trade. Pulls the trade's
    persisted context out of ``detail_json`` and runs the recall."""
    import json as _json
    with session_scope() as session:
        trade = session.get(Trade, trade_id)
        if trade is None:
            raise HTTPException(status_code=404, detail="trade not found")
        try:
            detail = _json.loads(trade.detail_json or "{}") or {}
        except Exception:
            detail = {}
        ticker = trade.ticker
        action = trade.action
        context = {
            "trade_id": trade_id,
            "ticker": ticker,
            "action": action,
            "analytics": detail.get("analytics"),
            "features": ((detail.get("analytics") or {}).get("features")
                            or detail.get("snapshot")),
        }
    matches = recall_similar(context, k=k)
    return {
        "trade_id": trade_id, "ticker": ticker, "action": action,
        "matches": [m.to_dict() for m in matches],
        "summary": recall_summary(matches),
    }
