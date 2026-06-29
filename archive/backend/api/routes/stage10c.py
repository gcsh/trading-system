"""Stage-10 items 7 / 9 / 13 endpoints — beta guardrail, sweep frontier,
IV-aware exits."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel


# ── beta guardrail ───────────────────────────────────────────────────────


beta_router = APIRouter(prefix="/portfolio/beta-guardrail", tags=["portfolio-optimizer"])


@beta_router.get("/preview")
async def beta_guardrail_preview(
    net_beta: float = Query(...),
    vol_label: str = Query(...),
    beta_threshold: Optional[float] = Query(None),
) -> dict:
    from backend.bot.portfolio_optimizer.beta_guardrail import (
        evaluate_beta_guardrail,
    )
    return evaluate_beta_guardrail(
        net_beta=net_beta, vol_label=vol_label,
        beta_threshold=beta_threshold,
    ).to_dict()


@beta_router.get("/live")
async def beta_guardrail_live() -> dict:
    """Read live net_beta from /portfolio/risk + vol from /cross-asset/state."""
    from backend.bot.cross_asset import fetch_state
    from backend.bot.portfolio_intel import assess_portfolio
    from backend.bot.portfolio_optimizer.beta_guardrail import (
        evaluate_beta_guardrail,
    )
    # Quick portfolio + cross-asset snapshot
    try:
        import backend.main as main_mod
        engine = getattr(main_mod.app.state, "engine", None)
        positions = engine.executor.positions() if engine else []
    except Exception:
        positions = []
    risk = assess_portfolio(positions or []).to_dict()
    state = fetch_state().to_dict()
    decision = evaluate_beta_guardrail(
        net_beta=float(risk.get("net_beta") or 0.0),
        vol_label=str(state.get("volatility") or ""),
    )
    return {
        "net_beta": risk.get("net_beta"),
        "vol_label": state.get("volatility"),
        "decision": decision.to_dict(),
    }


# ── IV-aware exits ──────────────────────────────────────────────────────


iv_router = APIRouter(prefix="/exits/iv-aware", tags=["exits"])


class IVExitsBody(BaseModel):
    take_profit_pct: float
    stop_loss_pct: float
    iv_rank: Optional[float] = None
    earnings_days: Optional[float] = None
    opex_week: bool = False


@iv_router.post("/preview")
async def iv_exit_preview(body: IVExitsBody) -> dict:
    from backend.bot.exits.iv_aware import adjust_tp_sl_for_iv_crush
    return adjust_tp_sl_for_iv_crush(
        take_profit_pct=body.take_profit_pct,
        stop_loss_pct=body.stop_loss_pct,
        iv_rank=body.iv_rank,
        earnings_days=body.earnings_days,
        opex_week=body.opex_week,
    ).to_dict()


# ── threshold sweep frontier ────────────────────────────────────────────


sweep_router = APIRouter(prefix="/sweeps", tags=["sweeps"])


@sweep_router.get("/frontier")
async def sweep_frontier(
    max_dd_cap_pct: float = Query(0.15, gt=0, le=1.0),
    min_trades: int = Query(10, ge=1, le=1000),
    limit_labels: int = Query(5000, ge=10, le=20000),
) -> dict:
    """Walk the live labelled trades and return the (grade × prob_floor)
    frontier + suggested config diff."""
    from backend.api.routes.metrics import _load_labels
    from backend.bot.sweeps import sweep_threshold_frontier
    labels = _load_labels(limit=limit_labels)
    result = sweep_threshold_frontier(
        labels, max_dd_cap_pct=max_dd_cap_pct, min_trades=min_trades,
    )
    return result.to_dict()


class SweepBody(BaseModel):
    labels: List[Dict[str, Any]]
    max_dd_cap_pct: float = 0.15
    min_trades: int = 10


@sweep_router.post("/frontier")
async def sweep_frontier_post(body: SweepBody) -> dict:
    """Same sweep but takes labels in the body — for tests + ad-hoc."""
    from backend.bot.labeling import TradeLabel
    from backend.bot.sweeps import sweep_threshold_frontier
    labels = [TradeLabel(**l) for l in body.labels]
    result = sweep_threshold_frontier(
        labels, max_dd_cap_pct=body.max_dd_cap_pct,
        min_trades=body.min_trades,
    )
    return result.to_dict()
