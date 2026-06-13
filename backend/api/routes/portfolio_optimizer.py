"""Stage-6 portfolio-optimizer endpoints."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from backend.bot.portfolio_optimizer import (
    allocate_capital,
    check_cluster_cap,
    cluster_exposures,
    cvar_size_fraction,
    drawdown_size_multiplier,
    kelly_fraction,
    optimize_size,
    vol_target_fraction,
)

router = APIRouter(prefix="/portfolio/optimizer", tags=["portfolio-optimizer"])


@router.get("/allocation")
async def allocation_view(request: Request) -> dict:
    """Per-strategy capital allocation using live ``/metrics/by-strategy`` data."""
    from backend.api.routes.metrics import build_summary
    from backend.bot.metrics import expectancy, profit_factor, win_rate
    from backend.bot.labeling import build_labels
    from sqlalchemy import desc, select
    from backend.db import session_scope
    from backend.models.decision_log import DecisionLog
    from backend.models.trade import Trade

    # Load + bucket — same logic as /metrics/by-strategy.
    # CRITICAL: never let the synthetic backfill corpus influence live
    # capital allocation — the allocator would dump real money into a
    # strategy whose stats came from a backtest, not from a live trade.
    with session_scope() as session:
        trade_rows = session.execute(
            select(Trade)
            .where(Trade.status != "closed_by_reset")
            .where(Trade.signal_source != "historical_replay")
            .order_by(desc(Trade.timestamp)).limit(5000)
        ).scalars().all()
        trades = [r.to_dict() for r in trade_rows]
        decision_rows = session.execute(
            select(DecisionLog).order_by(desc(DecisionLog.timestamp)).limit(5000)
        ).scalars().all()
        decisions = [r.to_dict() for r in decision_rows]
    labels = build_labels(trades, decisions)
    by_strategy: Dict[str, Dict[str, Any]] = {}
    for l in labels:
        pnls = []  # will collect per strategy
    # Group by strategy
    grouped: Dict[str, List] = {}
    for l in labels:
        grouped.setdefault(l.strategy or "—", []).append(l)
    for name, items in grouped.items():
        pnls = [l.pnl for l in items if l.pnl is not None]
        avg_win = (sum(p for p in pnls if p > 0)
                    / max(1, sum(1 for p in pnls if p > 0)))
        avg_loss = (sum(p for p in pnls if p < 0)
                     / max(1, sum(1 for p in pnls if p < 0))) if any(p < 0 for p in pnls) else 0.0
        by_strategy[name] = {
            "count": len(items),
            "closed": len(pnls),
            "win_rate": win_rate(pnls),
            "expectancy": expectancy(pnls),
            "profit_factor": profit_factor(pnls),
            "avg_win": round(avg_win, 2) if avg_win else None,
            "avg_loss": round(avg_loss, 2) if avg_loss else None,
        }
    allocations = allocate_capital(by_strategy)
    return {"allocations": [a.to_dict() for a in allocations],
             "cash_reserve_pct": round(max(0.0, 1.0 - sum(a.share for a in allocations)), 4),
             "by_strategy_metrics": by_strategy}


@router.get("/clusters")
async def clusters_view(request: Request) -> dict:
    """Current cluster exposure based on live paper positions."""
    engine = getattr(request.app.state, "engine", None)
    positions: List[Dict[str, Any]] = []
    equity = 0.0
    if engine is not None:
        try:
            positions = engine.executor.positions() or []
            state = engine.executor.get_account_state() or {}
            equity = float(state.get("portfolio_value") or 0.0)
        except Exception:
            pass
    exposures = cluster_exposures(positions, equity=equity)
    return {"equity": round(equity, 2),
             "clusters": [c.to_dict() for c in exposures]}


@router.get("/cluster-check")
async def cluster_check_endpoint(
    request: Request,
    ticker: str = Query(...),
    new_value: float = Query(..., gt=0),
) -> dict:
    engine = getattr(request.app.state, "engine", None)
    positions: List[Dict[str, Any]] = []
    equity = 0.0
    if engine is not None:
        try:
            positions = engine.executor.positions() or []
            state = engine.executor.get_account_state() or {}
            equity = float(state.get("portfolio_value") or 0.0)
        except Exception:
            pass
    res = check_cluster_cap(ticker=ticker, new_value=new_value,
                              positions=positions, equity=equity)
    return res.to_dict()


@router.get("/sizing/primitives")
async def sizing_primitives(
    win_rate: float = Query(0.5, ge=0, le=1),
    avg_win: float = Query(100.0),
    avg_loss: float = Query(-80.0),
    equity: float = Query(10_000, gt=0),
    daily_loss_budget: float = Query(200, gt=0),
    sigma_pct: float = Query(0.20, gt=0),
    target_vol: float = Query(0.15, gt=0),
    drawdown_pct: float = Query(0.0, ge=0),
) -> dict:
    """One-shot diagnostic returning every sizing primitive for a hypothetical
    trade. Helps the UI explain a final number from the optimizer."""
    return {
        "kelly_fraction": kelly_fraction(
            win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss),
        "cvar_fraction": cvar_size_fraction(
            equity=equity, daily_loss_budget=daily_loss_budget,
            sigma_pct=sigma_pct),
        "vol_target_fraction": vol_target_fraction(
            target_vol=target_vol, asset_vol=sigma_pct),
        "drawdown_multiplier": drawdown_size_multiplier(
            current_drawdown_pct=drawdown_pct),
    }


class OptimizePreviewBody(BaseModel):
    ticker: str
    strategy: str
    requested_dollar: float
    equity: float
    drawdown_pct: float = 0.0
    positions: List[Dict[str, Any]] = []
    by_strategy_metrics: Optional[Dict[str, Dict[str, Any]]] = None
    asset_volatility: Optional[float] = None
    daily_loss_budget: Optional[float] = None


@router.post("/preview")
async def preview(body: OptimizePreviewBody) -> dict:
    """Preview optimizer decision for a hypothetical plan."""
    decision = optimize_size(
        ticker=body.ticker, strategy=body.strategy,
        requested_dollar=body.requested_dollar, equity=body.equity,
        drawdown_pct=body.drawdown_pct, positions=body.positions,
        by_strategy_metrics=body.by_strategy_metrics,
        asset_volatility=body.asset_volatility,
        daily_loss_budget=body.daily_loss_budget,
    )
    return decision.to_dict()
