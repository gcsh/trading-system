"""Stage-10 items 17 / 18 / 19 / 20 endpoints — extra features."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


# ── regime extras (dGEX/dPrice + vol-of-vol) ─────────────────────────────


regime_router = APIRouter(prefix="/features/regime-extra", tags=["features"])


class GexSlopeBody(BaseModel):
    snapshots: List[Dict[str, Any]]
    min_obs: int = 3


@regime_router.post("/dgex-dprice")
async def dgex_dprice(body: GexSlopeBody) -> dict:
    from backend.bot.features.regime_extra import gex_dprice_slope
    slope = gex_dprice_slope(body.snapshots, min_obs=body.min_obs)
    return {"slope": slope, "n_obs": len(body.snapshots)}


class VolOfVolBody(BaseModel):
    returns: List[float]
    inner_window: int = 12
    outer_window: int = 24


@regime_router.post("/vol-of-vol")
async def vol_of_vol(body: VolOfVolBody) -> dict:
    from backend.bot.features.regime_extra import intraday_vol_of_vol
    value = intraday_vol_of_vol(
        body.returns,
        inner_window=body.inner_window,
        outer_window=body.outer_window,
    )
    return {"vol_of_vol": value,
             "n_returns": len(body.returns)}


# ── strike quality ───────────────────────────────────────────────────────


quality_router = APIRouter(prefix="/options/strike-quality", tags=["options"])


class StrikeQualityBody(BaseModel):
    quotes: List[Dict[str, Any]]
    strike: float
    kind: str
    expiration: Optional[str] = None


@quality_router.post("")
async def score(body: StrikeQualityBody) -> dict:
    from backend.bot.options_chain.strike_quality import score_strike
    return score_strike(
        body.quotes, strike=body.strike, kind=body.kind,
        expiration=body.expiration,
    ).to_dict()


# ── sweep/absorb momentum ────────────────────────────────────────────────


momo_router = APIRouter(prefix="/microstructure/momentum", tags=["microstructure"])


class MomentumBody(BaseModel):
    snapshots: List[Dict[str, Any]]
    min_obs: int = 5
    strong_slope: float = 0.05


@momo_router.post("")
async def sweep_absorb_momentum(body: MomentumBody) -> dict:
    from backend.bot.microstructure.momentum import (
        sweep_absorb_momentum as _impl,
    )
    result = _impl(
        body.snapshots, min_obs=body.min_obs,
        strong_slope=body.strong_slope,
    )
    if result is None:
        raise HTTPException(
            status_code=422,
            detail=(f"need ≥ {body.min_obs} snapshots with both "
                     "sweep_probability and absorption_probability"),
        )
    return result.to_dict()
