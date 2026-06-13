"""MITS Phase 3 — Tomorrow's Setup API.

Endpoints:
  GET  /tomorrow?date=YYYY-MM-DD&limit=20   — rank-ordered EOD analysis rows.
  POST /tomorrow/rebuild?date=YYYY-MM-DD    — manual trigger of run_eod_pass.

The EOD pass itself runs from the scheduler at 16:30 ET weekdays. The
POST endpoint is for backfill / re-runs / smoke checks.
"""
from __future__ import annotations

import logging
import threading
from datetime import date as _date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import desc, select

from backend.db import session_scope
from backend.models.eod_analysis import EodAnalysis

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tomorrow", tags=["tomorrow"])


def _parse_date(d: Optional[str]) -> _date:
    if not d:
        return datetime.utcnow().date()
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400,
                              detail="date must be YYYY-MM-DD")


@router.get("")
async def list_tomorrow(
    date: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    """Return rank-ordered EodAnalysis rows for the given date.

    Empty `rows` means the EOD pass hasn't been run for that date yet
    (the UI shows the "Next EOD pass at 16:30 ET" empty state).
    """
    target = _parse_date(date)
    with session_scope() as s:
        rows = s.execute(
            select(EodAnalysis)
            .where(EodAnalysis.analysis_date == target)
            .order_by(desc(EodAnalysis.rank_score))
            .limit(int(limit))
        ).scalars().all()
        out = [r.to_dict() for r in rows]
    return {
        "analysis_date": target.isoformat(),
        "rows": out,
        "count": len(out),
    }


@router.post("/rebuild")
async def rebuild_tomorrow(date: Optional[str] = None) -> Dict[str, Any]:
    """Manual trigger of `run_eod_pass` for the given date (or today).

    Runs asynchronously on a daemon thread so the HTTP response returns
    immediately. Caller should poll GET /tomorrow to see results.
    """
    target = _parse_date(date)

    def _run() -> None:
        try:
            from backend.bot.eod_analysis import run_eod_pass
            stats = run_eod_pass(target)
            logger.info("tomorrow rebuild %s: %s", target, stats)
        except Exception:
            logger.exception("tomorrow rebuild %s failed", target)

    threading.Thread(target=_run, name=f"tomorrow-rebuild-{target}",
                          daemon=True).start()
    return {
        "analysis_date": target.isoformat(),
        "status": "started",
    }
