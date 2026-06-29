"""Stage-19 — Earnings Call Intelligence endpoints.

  • ``GET  /earnings-intel/{ticker}``         — cached latest call intel
  • ``GET  /earnings-intel/{ticker}/history`` — last N calls
  • ``POST /earnings-intel/{ticker}/refresh`` — pull recent 8-K item-2.02, analyze
  • ``POST /earnings-intel/analyze``          — analyze pasted text (operator path)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from backend.bot.data.edgar import recent_filings_cached, refresh_ticker
from backend.bot.earnings_intel import analyze, heuristic_extract, history_for, latest_for
from backend.bot.earnings_intel.fetcher import fetch_release

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/earnings-intel", tags=["earnings_intel"])


@router.get("/{ticker}")
async def latest(ticker: str) -> dict:
    return {"ticker": ticker.upper(), "intel": latest_for(ticker)}


@router.get("/{ticker}/history")
async def history(ticker: str, limit: int = Query(8, ge=1, le=40)) -> dict:
    return {"ticker": ticker.upper(),
              "history": history_for(ticker, limit=limit)}


@router.post("/{ticker}/refresh")
async def refresh(ticker: str,
                     max_filings: int = Query(2, ge=1, le=10),
                     within_days: int = Query(180, ge=1, le=730),
                     prefer_claude: bool = Query(True)) -> dict:
    """Pull recent 8-K item-2.02 filings, fetch each press release from
    EDGAR, and analyze. Returns the analyses processed in this call."""
    # Ensure we have fresh metadata for the ticker.
    try:
        refresh_ticker(ticker, limit=10)
    except Exception:
        pass

    cutoff = datetime.utcnow() - timedelta(days=within_days)
    filings = recent_filings_cached(ticker, limit=40, forms=("8-K",))
    processed = []
    for f in filings:
        items = f.get("items") or ""
        if "2.02" not in items:
            continue
        try:
            filed_at = datetime.fromisoformat(f.get("filed_at"))
        except Exception:
            continue
        if filed_at < cutoff:
            continue
        result = fetch_release(
            cik=f.get("cik"),
            accession_number=f.get("accession_number"),
            primary_document=f.get("primary_document"),
        )
        if result is None:
            processed.append({
                "accession_number": f.get("accession_number"),
                "filed_at": f.get("filed_at"),
                "status": "no_exhibit",
            })
            continue
        text, exhibit_url = result
        intel = analyze(
            ticker=ticker, accession_number=f.get("accession_number"),
            filed_at=filed_at, text=text, prefer_claude=prefer_claude,
        )
        intel["exhibit_url"] = exhibit_url
        processed.append({"status": "analyzed", **intel})
        if len(processed) >= max_filings:
            break
    return {"ticker": ticker.upper(), "processed": processed}


class AnalyzeBody(BaseModel):
    ticker: str
    accession_number: Optional[str] = None    # falls back to a hash of the text
    filed_at: Optional[str] = None            # ISO date; defaults to now
    text: str
    prefer_claude: bool = True


@router.post("/analyze")
async def analyze_endpoint(body: AnalyzeBody) -> dict:
    """Operator path — paste a transcript / press release directly. Useful
    for tickers where the 8-K exhibit isn't where we expect, or for
    transcripts from sources other than EDGAR."""
    import hashlib
    acc = body.accession_number or hashlib.sha256(
        (body.text or "")[:512].encode()).hexdigest()[:18]
    try:
        filed = datetime.fromisoformat(body.filed_at) if body.filed_at \
            else datetime.utcnow()
    except Exception:
        filed = datetime.utcnow()
    intel = analyze(ticker=body.ticker, accession_number=acc,
                      filed_at=filed, text=body.text,
                      prefer_claude=body.prefer_claude)
    return {"ticker": body.ticker.upper(), "intel": intel}
