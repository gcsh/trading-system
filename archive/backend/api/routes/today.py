"""Today-snapshot endpoints — the single-source-of-truth surface for "what
happened so far today?"

Built on ``backend.bot.today_pnl.compute_today_pnl`` so every caller
(the v2 mission-control tile, /bot/status, /portfolio/performance) gets the
same realized + unrealized number."""
from __future__ import annotations

from fastapi import APIRouter

from backend.bot.today_pnl import compute_today_pnl_with_session

router = APIRouter(prefix="/today", tags=["today"])


@router.get("/summary")
async def summary() -> dict:
    """Return today's realized + unrealized + total P&L.

    Three keys, all floats:

    * ``realized_today``    closed-trade pnl recorded today (live rows only)
    * ``unrealized_today``  open-position MTM minus entry cost basis
    * ``total_today``       sum of the two

    The compute is best-effort: a failure in one half (e.g. a degraded
    options quote source) still returns the other half rather than 500-ing
    the whole tile."""
    return compute_today_pnl_with_session()
