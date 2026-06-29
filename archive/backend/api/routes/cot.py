"""Stage-18b — CFTC COT endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from backend.bot.data.cot import (
    INSTRUMENTS,
    latest_for,
    positioning_snapshot,
    refresh,
)

router = APIRouter(prefix="/cot", tags=["cot"])


@router.get("/snapshot")
async def snapshot() -> dict:
    return {"positioning": positioning_snapshot(),
              "instruments": list(INSTRUMENTS.values())}


@router.get("/instrument/{name}")
async def by_instrument(name: str) -> dict:
    return {"instrument": name.upper(), "latest": latest_for(name)}


@router.post("/refresh")
async def refresh_endpoint(year: Optional[int] = Query(None)) -> dict:
    return refresh(year=year)
