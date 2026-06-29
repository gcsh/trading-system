"""Alert history endpoints."""
from __future__ import annotations

from fastapi import APIRouter

from backend.bot.alerts import ALERT_CENTER

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("/list")
async def list_alerts(limit: int = 50) -> list[dict]:
    return ALERT_CENTER.recent(limit=limit)
