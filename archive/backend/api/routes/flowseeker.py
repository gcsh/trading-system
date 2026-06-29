"""Flowseeker (institutional options flow) endpoints."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter

from backend.bot.signals.flow import darkpool, flow_for, live_flow, summary
from backend.db import session_scope
from backend.models.config import load_config

router = APIRouter(prefix="/flow", tags=["flow"])


def _default_tickers() -> List[str]:
    try:
        with session_scope() as session:
            return (load_config(session).get("tickers") or ["SPY"])
    except Exception:
        return ["SPY"]


@router.get("/live")
async def live() -> List[dict]:
    """Most-urgent unusual flow across the configured universe."""
    return [a.to_dict() for a in live_flow(_default_tickers())]


@router.get("/darkpool")
async def darkpool_route() -> List[dict]:
    return darkpool()


@router.get("/summary")
async def summary_route() -> dict:
    return summary(live_flow(_default_tickers()))


@router.get("/{ticker}")
async def flow_ticker(ticker: str) -> List[dict]:
    return [a.to_dict() for a in flow_for(ticker.upper())]
