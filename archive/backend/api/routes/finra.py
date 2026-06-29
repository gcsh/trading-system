"""Stage-18b — FINRA short interest endpoints."""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from backend.bot.data.finra import latest_for, refresh, short_pressure

router = APIRouter(prefix="/finra", tags=["finra"])


@router.get("/short-interest/{ticker}")
async def short_interest(ticker: str) -> dict:
    return {"ticker": ticker.upper(), "latest": latest_for(ticker),
              "pressure": short_pressure(ticker)}


class RefreshBody(BaseModel):
    tickers: Optional[List[str]] = None      # filter to watchlist subset
    target_date: Optional[str] = None         # ISO date; default = today


@router.post("/refresh")
async def refresh_endpoint(body: Optional[RefreshBody] = None) -> dict:
    body = body or RefreshBody()
    target = None
    if body.target_date:
        try:
            target = date.fromisoformat(body.target_date)
        except Exception:
            pass
    return refresh(tickers=body.tickers, target_date=target)
