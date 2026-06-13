"""MITS Phase 6 (P6.4) — Weekly retrospective routes."""
from __future__ import annotations

import logging
from datetime import date as _date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from backend.bot.retrospective import (
    build_weekly_retrospective, monday_of_week,
)
from backend.db import session_scope
from backend.models.weekly_retrospective import WeeklyRetrospective

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/retrospective", tags=["retrospective"])


def _parse_week(week: Optional[str]) -> _date:
    if not week:
        # Default to the MOST RECENT completed Monday (the prior week).
        today = _date.today()
        this_monday = today - timedelta(days=today.weekday())
        return this_monday - timedelta(days=7)
    try:
        return _date.fromisoformat(week)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="week must be ISO date (YYYY-MM-DD)",
        )


@router.get("")
async def get_retrospective(week: Optional[str] = Query(None),
                                          rebuild: bool = Query(False),
                                          ) -> Dict[str, Any]:
    """Return the stored retrospective for the given Monday. Pass
    ``rebuild=true`` to recompute on demand (useful when the operator
    has wrapped up the week early and wants to see it now).
    """
    monday = monday_of_week(_parse_week(week))
    if rebuild:
        try:
            build_weekly_retrospective(monday)
        except Exception:
            logger.debug("rebuild retro failed", exc_info=True)
    with session_scope() as s:
        row = s.execute(
            select(WeeklyRetrospective)
            .where(WeeklyRetrospective.week_start_date == monday)
        ).scalar_one_or_none()
        if row is None:
            return {
                "week_start_date": monday.isoformat(),
                "present": False,
                "message": "No retrospective yet — Sunday cron hasn't run.",
            }
        out = row.to_dict()
        out["present"] = True
        return out


@router.get("/list")
async def list_retrospectives(limit: int = Query(12, ge=1, le=52)
                                          ) -> List[Dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(
            select(WeeklyRetrospective)
            .order_by(WeeklyRetrospective.week_start_date.desc())
            .limit(limit)
        ).scalars().all()
        return [r.to_dict() for r in rows]
