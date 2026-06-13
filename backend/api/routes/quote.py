"""MITS Phase 10.1 — live tick endpoint.

A single ultra-light ``GET /quote/{ticker}`` returning the freshest
available price plus provenance. Backed by the unified
``backend.bot.data.quote_source.get_quote()`` resolver (ThetaData →
Alpaca → yfinance), with a 500ms in-process cache so a 1s frontend poll
costs essentially nothing.

Why a dedicated endpoint?

  * The Theory Studio's Live toggle previously re-fetched the entire
    multi-theory annotation every 30 s — too slow to look like a "live"
    tape but too heavy to call every second. This endpoint splits the
    two concerns: the heavy theory analysis still re-runs at 30 s, but
    the chart's last-candle close updates on a 1 s tick from here.
  * ``/market/quote/{ticker}`` already exists but its payload shape is
    tuned for the Market overview page (yfinance-first, no ``ts``, no
    age tag). We keep that one stable and add this purpose-built one.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from fastapi import APIRouter, HTTPException


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/quote", tags=["quote"])


_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL_SEC = 0.5  # 500 ms — a 1s poll never hits the data layer twice.


@router.get("/{ticker}")
def quote(ticker: str) -> Dict[str, Any]:
    """Live tick. Returns ``{ticker, price, ts, source, age_seconds}``.

    ``age_seconds`` is the data layer's reported staleness (None if the
    provider didn't return a wallclock). ``ts`` is ALWAYS the server's
    response timestamp so the frontend can detect a frozen feed.
    """
    sym = (ticker or "").upper().strip()
    if not sym:
        raise HTTPException(status_code=400, detail="ticker required")
    now = time.monotonic()
    hit = _CACHE.get(sym)
    if hit and (now - hit[0]) < _CACHE_TTL_SEC:
        out = dict(hit[1])
        # Refresh the response-side timestamp so callers can still see a
        # heartbeat even on a cached fetch.
        out["ts"] = datetime.now(timezone.utc).isoformat()
        out["cached"] = True
        return out
    try:
        from backend.bot.data.quote_source import get_quote
        q = get_quote(sym)
    except Exception as exc:  # noqa: BLE001
        logger.exception("quote_source.get_quote failed for %s", sym)
        raise HTTPException(status_code=500, detail=str(exc))
    payload: Dict[str, Any] = {
        "ticker": sym,
        "price": float(q.price or 0.0),
        "source": q.source,
        "age_seconds": q.age_seconds,
        "ts": datetime.now(timezone.utc).isoformat(),
        "cached": False,
    }
    _CACHE[sym] = (now, payload)
    if len(_CACHE) > 512:
        # Drop oldest 25%.
        items = sorted(_CACHE.items(), key=lambda kv: kv[1][0])
        for k, _ in items[:128]:
            _CACHE.pop(k, None)
    return payload
