"""P2.3 — IV regime classifier endpoints.

  • ``GET /iv-regime/{ticker}``     — current regime for one ticker
  • ``GET /iv-regime/universe/all`` — universe regime map
  • ``POST /iv-regime/cache/reset`` — wipe the in-process classifier cache
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException

from backend.bot.iv_regime import (
    classify_ticker,
    regime_for_universe,
    reset_cache,
)

router = APIRouter(prefix="/iv-regime", tags=["iv-regime"])


@router.get("/universe/all")
async def universe_regime() -> dict:
    """Regime for every ticker in the scan universe + watchlist."""
    from backend.db import session_scope
    from backend.models.config import load_config
    from backend.models.watchlist import WatchlistItem
    with session_scope() as session:
        cfg = [t.upper().strip() for t in
                  (load_config(session).get("tickers") or []) if t]
        wl = [w.ticker.upper().strip()
                for w in session.query(WatchlistItem).all()
                if w.ticker and w.ticker.strip()]
    seen: set = set()
    tickers: List[str] = []
    for t in cfg + wl:
        if t and t not in seen:
            seen.add(t)
            tickers.append(t)
    return {
        "universe": tickers,
        "regimes": {k: v.to_dict() for k, v in regime_for_universe(tickers).items()},
    }


@router.get("/{ticker}")
async def one_ticker(ticker: str, force: bool = False) -> dict:
    """Current IV regime for ``ticker``. ``force=true`` bypasses the
    1-hour classifier cache (use sparingly — recompute touches the DB)."""
    report = classify_ticker(ticker.upper(), force=force)
    return report.to_dict()


@router.post("/cache/reset")
async def cache_reset() -> dict:
    reset_cache()
    return {"reset": True}
