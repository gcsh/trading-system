"""Stage-10 items 6 + 8 endpoints — theme heat + event-risk decay."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Query

from backend.bot.cohort_matrix.theme_heat import (
    compute_theme_heat,
    theme_size_multiplier,
)
from backend.bot.event_risk.decay import (
    can_trade_with_decay,
    decay_multiplier,
)


# ── theme heat ───────────────────────────────────────────────────────────


heat_router = APIRouter(prefix="/cohorts/theme-heat", tags=["cohorts"])


@heat_router.get("")
async def theme_heat(recent_n: int = Query(50, ge=5, le=500)) -> dict:
    heats = compute_theme_heat(recent_n=recent_n)
    return {"recent_n": recent_n,
             "heats": [h.to_dict() for h in heats]}


@heat_router.get("/{ticker}")
async def theme_heat_for_ticker(ticker: str,
                                   recent_n: int = Query(50, ge=5, le=500)) -> dict:
    mult = theme_size_multiplier(ticker, recent_n=recent_n)
    return {"ticker": ticker.upper(), "recent_n": recent_n,
             "size_multiplier": mult}


# ── event-risk decay ─────────────────────────────────────────────────────


decay_router = APIRouter(prefix="/event-risk/decay", tags=["event-risk"])


@decay_router.get("")
async def decay_now() -> dict:
    """What's the current post-event decay multiplier for ANY ticker?
    (decay applies portfolio-wide because macro events affect all names)."""
    return decay_multiplier().to_dict()


@decay_router.get("/{ticker}")
async def decay_for_ticker(ticker: str) -> dict:
    """Combined hard-hold + post-event decay for a specific ticker."""
    return can_trade_with_decay(ticker=ticker)
