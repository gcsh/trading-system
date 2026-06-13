"""Phase-1 Authority endpoints.

  • ``GET /authority/status`` — the Authority Spine payload
  • ``GET /authority/pillar/{name}`` — per-pillar drill-down
  • ``GET /authority/contract`` — published pillar contract definitions
  • ``GET /authority/scan-universe`` — resolved scan list
  • ``GET /system/warnings`` — in-memory WARNING/ERROR ring buffer
  • ``POST /system/warnings/clear`` — operator-acknowledge the buffer
  • ``GET /system/data-quality`` — options-data provider + sanity aggregates (P1.5)
  • ``POST /system/data-quality/reset`` — wipe the counters
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from backend.bot.authority import (
    AUTHORITY_LEVELS,
    AUTHORITY_CONFIDENCE,
    PILLAR_NAMES,
    PILLAR_VOCAB,
    get_authority_status,
    get_pillar_detail,
)
from backend.bot.warnings_log import handler as warnings_handler

router = APIRouter(prefix="/authority", tags=["authority"])

# Second router for /system/* — same file because they're operator
# observability endpoints and share import surface.
system_router = APIRouter(prefix="/system", tags=["system"])


@system_router.get("/warnings")
async def system_warnings(
    limit: int = Query(100, ge=1, le=500),
    level: str | None = Query(None),
) -> dict:
    """Recent WARNING+ records from the in-memory ring buffer.

    Newest first. Optional ``level`` filter to WARNING / ERROR /
    CRITICAL.
    """
    h = warnings_handler()
    return {
        "records": h.snapshot(limit=limit, level=level),
        "counts": h.counts(),
    }


@system_router.post("/warnings/clear")
async def system_warnings_clear() -> dict:
    """Operator-acknowledge: wipe the ring buffer."""
    n = warnings_handler().clear()
    return {"cleared": n}


@system_router.get("/data-quality")
async def system_data_quality() -> dict:
    """Options-data provider hits + sanity-flag aggregates since process
    start (P1.5). Lets the operator confirm ThetaData is the dominant
    source and see which sanity flags fire most often before re-enabling
    options trading."""
    from backend.bot.data.options import get_data_quality_aggregates
    return get_data_quality_aggregates()


@system_router.post("/data-quality/reset")
async def system_data_quality_reset() -> dict:
    """Wipe the counters — useful when investigating whether a new build
    actually fixed something."""
    from backend.bot.data.options import reset_data_quality_aggregates
    reset_data_quality_aggregates()
    return {"reset": True}


@router.get("/status")
async def authority_status() -> dict:
    """Spine payload. Safe to poll every few seconds."""
    return get_authority_status()


@router.get("/pillar/{name}")
async def authority_pillar(name: str) -> dict:
    """Full pillar signals + contract for a drill-down view."""
    detail = get_pillar_detail(name.lower())
    if detail is None:
        raise HTTPException(status_code=404,
                                 detail=f"unknown pillar {name!r}")
    return detail


@router.get("/scan-universe")
async def scan_universe() -> dict:
    """The exact list of tickers the engine will scan next cycle.

    Source: union(config.tickers, watchlist_items) — same rollup the
    engine uses. Surfaced so the operator can confirm what's actually
    being scanned without guessing.
    """
    from backend.db import session_scope
    from backend.models.config import load_config
    from backend.models.watchlist import WatchlistItem
    with session_scope() as session:
        config = load_config(session)
        wl = [w.ticker.upper().strip()
                for w in session.query(WatchlistItem).all()
                if w.ticker and w.ticker.strip()]
    cfg = [t.upper().strip() for t in (config.get("tickers") or []) if t]
    seen = set()
    universe = []
    for t in cfg + wl:
        if t and t not in seen:
            seen.add(t)
            universe.append({
                "ticker": t,
                "source": "config" if t in cfg else "watchlist",
            })
    return {
        "tickers": [u["ticker"] for u in universe],
        "from_config": cfg,
        "from_watchlist": [t for t in wl if t not in cfg],
        "details": universe,
        "count": len(universe),
    }


@router.get("/contract")
async def authority_contract() -> dict:
    """Published pillar vocabularies + confidence states + level
    semantics. The operator can audit the rollup rules here."""
    return {
        "authority_levels": list(AUTHORITY_LEVELS),
        "authority_confidence": list(AUTHORITY_CONFIDENCE),
        "pillars": list(PILLAR_NAMES),
        "vocabulary": dict(PILLAR_VOCAB),
        "confidence_rules": {
            "CONFIDENT":  "all pillars 'ok' AND mean dissent ≤ 25%",
            "WATCHING":   "any 1 pillar 'mid' OR dissent in (25%, 40%] OR ≥3 unknown",
            "RESTRICTED": "any pillar 'bad' OR ≥ 2 'mid' OR dissent > 40%",
        },
        "dissent_bands": {
            "Normal":   "≤ 25%",
            "Elevated": "25-40%",
            "High":     "> 40%",
        },
        "promotion_separate": (
            "Promotion gates are NOT part of confidence. They live "
            "on the Promotion Readiness surface separately."
        ),
    }
