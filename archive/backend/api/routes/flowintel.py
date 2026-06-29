"""Institutional Flow Intelligence endpoint — dealer positioning + flow profile."""
from __future__ import annotations

from fastapi import APIRouter

from backend.bot.flowintel import analyze

router = APIRouter(prefix="/flowintel", tags=["flowintel"])


@router.get("/{ticker}")
async def flowintel_for(ticker: str) -> dict:
    """Dealer positioning (regime, pin probability, walls) + flow profile
    (sweep aggression, repeat orders, direction)."""
    return analyze(ticker.upper())
