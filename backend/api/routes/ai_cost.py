"""Stage-12.B6 AI Cost endpoints.

  • ``GET /ai-cost/summary``       — totals + per-surface rollup
  • ``GET /ai-cost/recent``        — last N entries
  • ``GET /ai-cost/alpha-ratio``   — $ profit per $ API spend (trade-attributed)
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from backend.bot.ai_cost import alpha_per_dollar, by_surface, recent_entries, totals

router = APIRouter(prefix="/ai-cost", tags=["ai_cost"])


@router.get("/summary")
async def summary() -> dict:
    return {
        "totals": totals(),
        "by_surface": by_surface(),
    }


@router.get("/recent")
async def recent(limit: int = Query(100, ge=1, le=1000)) -> dict:
    return {"entries": recent_entries(limit=limit)}


@router.get("/alpha-ratio")
async def alpha_ratio() -> dict:
    return alpha_per_dollar()
