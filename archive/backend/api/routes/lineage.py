"""Stage-11.2 Decision Lineage endpoint.

  • ``GET /lineage/trade/{id}`` — full decision chain for one trade
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.bot.lineage import build_lineage

router = APIRouter(prefix="/lineage", tags=["lineage"])


@router.get("/trade/{trade_id}")
async def trade_lineage(trade_id: int) -> dict:
    payload = build_lineage(trade_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="trade not found")
    return payload
