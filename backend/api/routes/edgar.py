"""Stage-18a — SEC EDGAR endpoints.

  • ``GET /edgar/filings/{ticker}``     — cached recent filings
  • ``GET /edgar/insider/{ticker}``     — Form 4 activity summary
  • ``GET /edgar/material/{ticker}``    — has-material-event boolean for the window
  • ``POST /edgar/refresh/{ticker}``    — pull fresh filings for one ticker
  • ``POST /edgar/refresh-universe``    — pull for an arbitrary watchlist
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from backend.bot.data.edgar import (
    has_material_event,
    insider_activity_summary,
    recent_filings_cached,
    refresh_ticker,
    refresh_universe,
)

router = APIRouter(prefix="/edgar", tags=["edgar"])


@router.get("/filings/{ticker}")
async def filings(ticker: str,
                     limit: int = Query(20, ge=1, le=200),
                     form: Optional[str] = Query(None,
                            description="filter to one form e.g. '8-K'")) -> dict:
    forms = (form,) if form else None
    rows = recent_filings_cached(ticker, limit=limit, forms=forms)
    return {"ticker": ticker.upper(), "filings": rows}


@router.get("/insider/{ticker}")
async def insider(ticker: str,
                     days: int = Query(30, ge=1, le=365)) -> dict:
    return insider_activity_summary(ticker, days=days)


@router.get("/material/{ticker}")
async def material(ticker: str,
                      within_hours: int = Query(48, ge=1, le=720)) -> dict:
    return {"ticker": ticker.upper(),
              "has_material_event": has_material_event(ticker, within_hours=within_hours),
              "within_hours": within_hours}


@router.post("/refresh/{ticker}")
async def refresh_one(ticker: str,
                         limit: int = Query(40, ge=1, le=200)) -> dict:
    return refresh_ticker(ticker, limit=limit)


class RefreshUniverseBody(BaseModel):
    tickers: List[str]
    limit_per_ticker: int = 20


@router.post("/refresh-universe")
async def refresh_many(body: RefreshUniverseBody) -> dict:
    return refresh_universe(body.tickers, limit_per_ticker=body.limit_per_ticker)
