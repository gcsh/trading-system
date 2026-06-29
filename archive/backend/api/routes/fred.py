"""Stage-18a — FRED endpoints.

  • ``GET /fred/snapshot`` — current value + 30-day change for every series in the canonical panel
  • ``GET /fred/series/{id}`` — history for one series
  • ``POST /fred/refresh`` — manually trigger a fetch (scheduler does this daily)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from backend.bot.data.fred import (
    CANONICAL_SERIES,
    FredClient,
    history,
    latest,
    macro_snapshot,
    refresh,
)

router = APIRouter(prefix="/fred", tags=["fred"])


@router.get("/snapshot")
async def snapshot() -> dict:
    return {"snapshot": macro_snapshot(),
             "series": list(CANONICAL_SERIES)}


@router.get("/series/{series_id}")
async def series(series_id: str,
                    limit: int = Query(252, ge=1, le=2000)) -> dict:
    rows = history(series_id, limit=limit)
    l = latest(series_id)
    return {
        "series_id": series_id,
        "latest": (l and {"date": l.date.isoformat(), "value": l.value}),
        "observations": [
            {"date": r.date.isoformat(), "value": r.value} for r in rows
        ],
    }


@router.post("/refresh")
async def refresh_endpoint(series: Optional[str] = Query(None,
        description="comma-separated FRED series IDs (default: canonical panel)")
                              ) -> dict:
    sids = [s.strip() for s in series.split(",")] if series else None
    return refresh(series=sids)
