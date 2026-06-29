"""Stage-10 items 10 / 14 / 15 / 16 endpoints —
quantile MFE/MAE, TWAP simulator, slippage shock, leakage canary."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel


# ── slippage shock sensitivity ──────────────────────────────────────────


shock_router = APIRouter(prefix="/backtest/shock", tags=["backtest"])


@shock_router.get("/{strategy}/{ticker}")
async def shock_sensitivity(strategy: str, ticker: str,
                              period: str = Query("6mo"),
                              interval: str = Query("1d"),
                              shocks: str = Query("0,5,10,20,40"),
                              min_sharpe: float = Query(1.0)) -> dict:
    from backend.bot.sensitivity import shock_sensitivity_grid
    try:
        shock_list = [float(s.strip()) for s in shocks.split(",") if s.strip()]
    except ValueError:
        raise HTTPException(status_code=400,
                              detail="shocks must be a comma list of floats")
    report = shock_sensitivity_grid(
        strategy_name=strategy, ticker=ticker.upper(),
        period=period, interval=interval, shocks_bps=shock_list,
        min_sharpe=min_sharpe,
    )
    return report.to_dict()


# ── leakage canary ──────────────────────────────────────────────────────


leakage_router = APIRouter(prefix="/ml/leakage", tags=["ml"])


class LagCanaryBody(BaseModel):
    model_type: str = "logistic"      # ensemble | hist_gb | logistic
    lag: int = 5
    tolerance: float = 0.05


@leakage_router.post("/canary")
async def leakage_canary(body: LagCanaryBody) -> dict:
    """Run the lag canary on the live feature store. Trains a fresh model
    twice (clean labels + lagged labels) and reports if the lagged model
    still beats baseline — that would indicate lookahead leakage."""
    from backend.bot.leakage import lag_canary
    from backend.bot.ml import create_model
    from backend.bot.ml.feature_store import build_dataset

    X, y, meta = build_dataset(min_closed=30)
    if X is None or y is None:
        raise HTTPException(status_code=422,
                              detail={"reason": "insufficient data",
                                       "feature_store": meta})
    try:
        model = create_model(body.model_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    report = lag_canary(model=model, X=X, y=y, lag=body.lag,
                          tolerance=body.tolerance)
    return {"feature_store": meta, "report": report.to_dict()}


# ── TWAP simulator ──────────────────────────────────────────────────────


twap_router = APIRouter(prefix="/execution/twap", tags=["execution"])


class TwapBody(BaseModel):
    side: str
    total_quantity: float
    bars: List[Dict[str, Any]]
    n_slices: int = 5
    base_slippage_bps: float = 2.0


@twap_router.post("/simulate")
async def twap_simulate(body: TwapBody) -> dict:
    from backend.bot.execution_sim.twap import simulate_twap
    return simulate_twap(
        side=body.side, total_quantity=body.total_quantity,
        bars=body.bars, n_slices=body.n_slices,
        base_slippage_bps=body.base_slippage_bps,
    ).to_dict()


class TwapCompareBody(TwapBody):
    market_slippage_bps: float = 25.0


@twap_router.post("/compare")
async def twap_compare(body: TwapCompareBody) -> dict:
    from backend.bot.execution_sim.twap import market_vs_twap
    return market_vs_twap(
        side=body.side, total_quantity=body.total_quantity,
        bars=body.bars, n_slices=body.n_slices,
        base_slippage_bps=body.base_slippage_bps,
        market_slippage_bps=body.market_slippage_bps,
    )


# ── quantile MFE/MAE exit suggestions ──────────────────────────────────


exit_models_router = APIRouter(prefix="/exits/mfe-mae", tags=["exits"])


class ExitSuggestBody(BaseModel):
    features: Dict[str, Any]
    fallback_tp_pct: float = 0.10
    fallback_sl_pct: float = 0.05
    version: str = "default"


@exit_models_router.post("/suggest")
async def exit_suggest(body: ExitSuggestBody) -> dict:
    from backend.bot.ml.exit_models import suggest_tp_sl
    return suggest_tp_sl(
        features_row=body.features,
        fallback_tp_pct=body.fallback_tp_pct,
        fallback_sl_pct=body.fallback_sl_pct,
        version=body.version,
    ).to_dict()


class TrainExitBody(BaseModel):
    quantile: float = 0.75
    version: str = "default"
    # Caller supplies synthetic targets when no per-trade MFE/MAE data
    # exists yet. In production, a nightly job would compute these from
    # closed trades.
    feature_rows: List[Dict[str, Any]]
    mfe_targets: List[float]
    mae_targets: List[float]


@exit_models_router.post("/train")
async def exit_train(body: TrainExitBody) -> dict:
    import pandas as pd
    from backend.bot.ml.exit_models import (
        save_exit_models, train_mfe_mae_models,
    )

    if not body.feature_rows:
        raise HTTPException(status_code=422,
                              detail="feature_rows must be non-empty")
    if len(body.mfe_targets) != len(body.feature_rows):
        raise HTTPException(status_code=422,
                              detail="targets length must match feature_rows")
    X = pd.DataFrame(body.feature_rows)
    mfe_m, mae_m = train_mfe_mae_models(
        X, body.mfe_targets, body.mae_targets, quantile=body.quantile,
    )
    paths = save_exit_models(mfe_m, mae_m, version=body.version)
    return {"version": body.version, **paths,
             "rows_trained": len(body.feature_rows)}
