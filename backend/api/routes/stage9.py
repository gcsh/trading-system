"""Stage-9 endpoints — abstain preview, loss autopsy, cohort matrix."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.bot.abstain import abstain_and_throttle
from backend.bot.autopsy import autopsy_recent_losses, autopsy_trade
from backend.bot.cohort_matrix import build_cohort_matrix, cohort_win_rate


# ── autopsy ───────────────────────────────────────────────────────────────


autopsy_router = APIRouter(prefix="/autopsy", tags=["autopsy"])


@autopsy_router.get("/trade/{trade_id}")
async def autopsy_one(trade_id: int) -> dict:
    bundle = autopsy_trade(trade_id)
    if bundle is None:
        raise HTTPException(
            status_code=404,
            detail="trade not found OR not a loss (autopsy is only for losses)",
        )
    return bundle.to_dict()


@autopsy_router.get("/recent")
async def autopsy_recent(limit: int = Query(50, ge=1, le=500)) -> dict:
    return autopsy_recent_losses(limit=limit)


# ── cohort matrix ────────────────────────────────────────────────────────


cohort_router = APIRouter(prefix="/cohorts", tags=["cohorts"])


@cohort_router.get("/matrix")
async def cohort_matrix(limit: int = Query(5000, ge=10, le=20000),
                          min_cohort_closed: int = Query(1, ge=1, le=100)
                          ) -> dict:
    return build_cohort_matrix(limit=limit, min_cohort_closed=min_cohort_closed)


@cohort_router.get("/rolling/{strategy}/{regime}")
async def cohort_rolling(strategy: str, regime: str,
                            recent_n: int = Query(30, ge=5, le=200)) -> dict:
    wr, n = cohort_win_rate(strategy, regime, recent_n=recent_n)
    return {"strategy": strategy, "regime": regime,
             "rolling_window": recent_n, "win_rate": wr, "n_closed": n}


# ── abstain preview ─────────────────────────────────────────────────────


abstain_router = APIRouter(prefix="/abstain", tags=["abstain"])


class AbstainPreviewBody(BaseModel):
    action: str
    probability: Optional[float] = None
    expected_move_pct: Optional[float] = None
    total_cost_bps: float = 0.0
    regime_label: Optional[str] = None
    snapshot: Optional[Dict[str, Any]] = None
    cohort_win_rate: Optional[float] = None
    cohort_closed: int = 0


@abstain_router.post("/preview")
async def abstain_preview(body: AbstainPreviewBody) -> dict:
    decision = abstain_and_throttle(
        action=body.action, probability=body.probability,
        expected_move_pct=body.expected_move_pct,
        total_cost_bps=body.total_cost_bps,
        regime_label=body.regime_label, snapshot=body.snapshot,
        cohort_win_rate=body.cohort_win_rate,
        cohort_closed=body.cohort_closed,
    )
    return decision.to_dict()
