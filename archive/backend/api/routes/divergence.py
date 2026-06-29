"""P3.5 — Divergence framework endpoint."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Query

from backend.bot.divergence import compute_divergence

router = APIRouter(prefix="/divergence", tags=["divergence"])


@router.get("/paper-vs-benchmark")
async def paper_vs_benchmark(
    hours: int = Query(168, ge=24, le=24 * 30,
                            description="Window in hours, default 7 days"),
) -> Dict[str, Any]:
    """Per-trade and daily-aggregate divergence between paper fills and
    a TastyTrade-style conservative fill benchmark.

    Returns ``alert: true`` when 7-day divergence > 5%, the threshold
    at which the paper fill model needs recalibration before drawing
    trial conclusions."""
    return compute_divergence(hours=hours)
