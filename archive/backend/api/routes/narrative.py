"""Narrative + Macro Intelligence endpoint.

Gathers recent headlines across the configured ticker universe and asks the
NarrativeAnalyzer for the dominant theme + beneficiaries + macro_risk. Caches
the result for the configured TTL so the cockpit can poll it cheaply.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter

from backend.bot.narrative import NarrativeAnalyzer, NarrativeState
from backend.db import session_scope
from backend.models.config import load_config

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/narrative", tags=["narrative"])

_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_TTL = 600.0   # 10 minutes — narrative shifts slowly; no need to hammer NewsAPI


def _gather_headlines(tickers: List[str], per_ticker: int = 4) -> List[str]:
    try:
        from backend.bot.signals.news import fetch_news
    except Exception:
        return []
    out: List[str] = []
    for ticker in tickers[:8]:                # cap to keep latency / API cost low
        try:
            articles = fetch_news(ticker, max_items=per_ticker) or []
            for art in articles:
                title = (art.get("title") or "").strip()
                if title:
                    out.append(title)
        except Exception:
            continue
    return out


@router.get("")
async def narrative_state() -> dict:
    """Live macro narrative — dominant theme, beneficiaries, risk label."""
    now = time.monotonic()
    cached = _CACHE.get("default")
    if cached and (now - cached[0]) < _TTL:
        return cached[1]

    with session_scope() as session:
        tickers = (load_config(session).get("tickers") or [])

    headlines = _gather_headlines(tickers)
    state: NarrativeState = NarrativeAnalyzer().analyze(headlines, universe=tickers)
    out = state.to_dict()
    out["headlines_seen"] = len(headlines)
    _CACHE["default"] = (now, out)
    return out
