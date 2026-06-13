"""Stage-10 endpoints — staged exits preview, adaptive spread, drift halts,
adaptive min_grade."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.bot.drift.auto_halt import (
    check_and_update_halts,
    clear_halt,
    halt_strategy,
    is_halted,
    list_halts,
)
from backend.bot.execution_costs.adaptive import (
    adaptive_spread_floor,
    spread_quantiles,
)
from backend.bot.exits import ExitState, evaluate_policies
from backend.bot.gates.adaptive import adaptive_min_grade


# ── exits ─────────────────────────────────────────────────────────────────


exits_router = APIRouter(prefix="/exits", tags=["exits"])


class ExitPreviewBody(BaseModel):
    entry_price: float
    current_price: float
    stop_pct: float = 0.05
    take_profit_pct: float = 0.10
    atr: float = 0.0
    state: Optional[Dict[str, Any]] = None
    opened_at: Optional[str] = None
    max_hold_minutes: Optional[int] = None


@exits_router.post("/policy/preview")
async def exits_preview(body: ExitPreviewBody) -> dict:
    """What would the staged-exit manager do RIGHT NOW given the inputs?"""
    state = ExitState(**(body.state or {}))
    if body.opened_at and not state.opened_at:
        state.opened_at = body.opened_at
    action = evaluate_policies(
        entry_price=body.entry_price,
        current_price=body.current_price,
        stop_pct=body.stop_pct,
        take_profit_pct=body.take_profit_pct,
        atr=body.atr,
        state=state,
        max_hold_minutes=body.max_hold_minutes,
    )
    return action.to_dict()


# ── adaptive spread ──────────────────────────────────────────────────────


spread_router = APIRouter(prefix="/execution/spread", tags=["execution"])


@spread_router.get("/quantiles/{ticker}")
async def spread_q(ticker: str, side: Optional[str] = None) -> dict:
    return spread_quantiles(ticker, side=side)


@spread_router.get("/adaptive-floor/{ticker}")
async def adaptive_floor(ticker: str,
                            side: Optional[str] = None,
                            quantile: float = Query(0.75, ge=0.5, le=0.99)
                            ) -> dict:
    return {
        "ticker": ticker.upper(),
        "side": side,
        "quantile": quantile,
        "adaptive_floor_bps": adaptive_spread_floor(
            ticker, side=side, quantile=quantile),
    }


# ── drift halts ──────────────────────────────────────────────────────────


halt_router = APIRouter(prefix="/drift/halts", tags=["drift"])


@halt_router.get("")
async def halts_index() -> dict:
    return {"halts": list_halts()}


@halt_router.get("/{strategy}")
async def halt_status(strategy: str) -> dict:
    return {"strategy": strategy, "halted": is_halted(strategy)}


class HaltBody(BaseModel):
    strategy: str
    feature: str
    psi_value: float
    reason: str = ""


@halt_router.post("")
async def post_halt(body: HaltBody) -> dict:
    return halt_strategy(strategy=body.strategy, feature=body.feature,
                           psi_value=body.psi_value, reason=body.reason)


@halt_router.delete("/{strategy}")
async def delete_halt(strategy: str) -> dict:
    cleared = clear_halt(strategy)
    if not cleared:
        raise HTTPException(status_code=404,
                              detail=f"no active halt for '{strategy}'")
    return {"cleared": strategy}


class CheckHaltsBody(BaseModel):
    baseline_by_strategy: Dict[str, Dict[str, List[float]]]
    current_by_strategy: Dict[str, Dict[str, List[float]]]
    psi_threshold: float = 0.25
    clear_threshold: float = 0.10


@halt_router.post("/check")
async def check_halts(body: CheckHaltsBody) -> dict:
    return check_and_update_halts(
        baseline_by_strategy=body.baseline_by_strategy,
        current_by_strategy=body.current_by_strategy,
        psi_threshold=body.psi_threshold,
        clear_threshold=body.clear_threshold,
    )


# ── adaptive min_grade ──────────────────────────────────────────────────


grade_router = APIRouter(prefix="/gates/grade", tags=["gates"])


@grade_router.get("/adaptive")
async def adaptive_grade(
    configured: Optional[str] = Query("C"),
    calibration_error: Optional[float] = Query(None),
    brier: Optional[float] = Query(None),
) -> dict:
    """Diagnostic — what would the engine pick for min_grade given inputs?"""
    return {
        "configured": configured,
        "calibration_error": calibration_error,
        "brier": brier,
        "effective_min_grade": adaptive_min_grade(
            configured_min_grade=configured,
            calibration_error=calibration_error,
            brier=brier,
        ),
    }


@grade_router.get("/live")
async def live_grade() -> dict:
    """Read live calibration from /metrics/summary and compute the adaptive
    floor the engine would use right now."""
    from backend.api.routes.metrics import build_summary
    from backend.db import session_scope
    from backend.models.config import load_config

    with session_scope() as session:
        cfg = load_config(session)
    configured = (cfg.get("analytics") or {}).get("min_grade") or "C"
    summary = build_summary()
    data = summary.get("data") or {}
    ece = data.get("calibration_error")
    brier = data.get("brier")
    effective = adaptive_min_grade(
        configured_min_grade=configured,
        calibration_error=ece, brier=brier,
    )
    return {
        "configured": configured,
        "live_calibration_error": ece,
        "live_brier": brier,
        "effective_min_grade": effective,
        "tightened": effective != configured,
    }
