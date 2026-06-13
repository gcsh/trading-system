"""Stage-18a — Market Breadth endpoints.

  • ``GET /breadth/latest``  — newest breadth snapshot
  • ``GET /breadth/history`` — last N daily snapshots
  • ``GET /breadth/health``  — one-line regime-health verdict
  • ``POST /breadth/refresh`` — manually trigger a recompute (scheduler does it daily)
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from backend.bot.breadth import (
    UNIVERSE,
    history,
    latest,
    refresh,
    regime_health,
)

router = APIRouter(prefix="/breadth", tags=["breadth"])


@router.get("/latest")
async def latest_endpoint() -> dict:
    l = latest()
    return {
        "snapshot": l.to_dict() if l else None,
        "universe_size": len(UNIVERSE),
    }


@router.get("/history")
async def history_endpoint(limit: int = Query(60, ge=1, le=500)) -> dict:
    return {"snapshots": history(limit=limit)}


@router.get("/health")
async def health() -> dict:
    return regime_health()


@router.post("/refresh")
async def refresh_endpoint() -> dict:
    return refresh()
