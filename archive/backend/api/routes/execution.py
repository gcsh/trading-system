"""Execution-quality endpoints — slippage telemetry per side/ticker."""
from __future__ import annotations

from fastapi import APIRouter, Query

from backend.bot.execution_intel import insights as exec_insights

router = APIRouter(prefix="/execution", tags=["execution"])


@router.get("/insights")
async def execution_insights(limit: int = Query(1000, ge=10, le=10000)) -> dict:
    """Average slippage (bps), adverse rate + per-side / per-ticker buckets."""
    return exec_insights(limit=limit)
