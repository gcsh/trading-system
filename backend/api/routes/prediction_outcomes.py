"""MITS Phase 5 (P5.2) — prediction→outcome API.

Endpoints:
  GET  /prediction-outcomes?date=YYYY-MM-DD&limit=50
       Rows ranked by analysis_date desc.
  GET  /prediction-outcomes/accuracy?window=7|30|all
       Aggregate accuracy metrics: % of high-conviction setups that
       traded, of those that traded what % won.
  POST /prediction-outcomes/reconcile?date=YYYY-MM-DD
       Manual trigger of the nightly reconcile job (operator + tests).

Read-only for the most part; the POST is for backfills.
"""
from __future__ import annotations

import logging
from datetime import date as _date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import desc, select

from backend.bot.eod_bias import accuracy_window, reconcile_outcomes
from backend.db import session_scope
from backend.models.eod_prediction_outcome import EodPredictionOutcome

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/prediction-outcomes", tags=["prediction-outcomes"])


def _parse_date(d: Optional[str]) -> Optional[_date]:
    if not d:
        return None
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400,
                              detail="date must be YYYY-MM-DD")


@router.get("")
async def list_prediction_outcomes(
    date: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
) -> Dict[str, Any]:
    target = _parse_date(date)
    with session_scope() as s:
        stmt = select(EodPredictionOutcome)
        if target is not None:
            stmt = stmt.where(EodPredictionOutcome.analysis_date == target)
        stmt = stmt.order_by(
            desc(EodPredictionOutcome.analysis_date),
            EodPredictionOutcome.id.asc(),
        ).limit(int(limit))
        rows: List[Dict[str, Any]] = [
            r.to_dict() for r in s.execute(stmt).scalars().all()
        ]
    return {
        "date": target.isoformat() if target else None,
        "rows": rows,
        "count": len(rows),
    }


@router.get("/accuracy")
async def accuracy(
    window: str = Query("30", description="7 | 30 | all (or any integer)")
) -> Dict[str, Any]:
    return accuracy_window(window)


@router.post("/reconcile")
async def reconcile(date: Optional[str] = None) -> Dict[str, Any]:
    target = _parse_date(date) or datetime.utcnow().date()
    stats = reconcile_outcomes(target)
    return {"date": target.isoformat(), **stats}
