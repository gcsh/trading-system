"""Stage-19 — Source Contribution endpoints.

  • ``GET /source-attribution/contributions`` — per-source contribution rollup
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from backend.bot.source_attribution import compute_contributions

router = APIRouter(prefix="/source-attribution", tags=["source_attribution"])


@router.get("/contributions")
async def contributions(limit: int = Query(5000, ge=50, le=20000),
                          min_trades: int = Query(30, ge=5, le=500)) -> dict:
    """Return the per-source contribution table. Each source's score is
    correlated against realized P&L over the last ``limit`` closed
    trades; sources need ≥ ``min_trades`` non-null scores before their r
    is reported. Below that they show in the table with `correlation:
    None` and an "insufficient data" insight."""
    return compute_contributions(limit=limit, min_trades=min_trades).to_dict()
