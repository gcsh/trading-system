"""Stage-3 options chain / IV-surface / Greeks / assignment-risk endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from backend.bot.greeks import compute_greeks, implied_vol
from backend.bot.options_chain import (
    assignment_probability,
    available_expirations,
    fetch_chain,
    iv_surface,
    nearest_available_strike,
)

router = APIRouter(prefix="/options", tags=["options"])


@router.get("/expirations/{ticker}")
async def expirations(ticker: str) -> dict:
    return {"ticker": ticker.upper(),
             "expirations": available_expirations(ticker)}


@router.get("/chain/{ticker}")
async def chain(ticker: str, expiration: Optional[str] = None,
                  prefer_synthetic: bool = Query(False)) -> dict:
    """Full chain. ``prefer_synthetic=true`` short-circuits the network call
    — useful for the UI to show a deterministic example without yfinance."""
    chain = fetch_chain(ticker, expiration=expiration,
                          prefer_synthetic=prefer_synthetic)
    return chain.to_dict()


@router.get("/iv-surface/{ticker}")
async def get_iv_surface(ticker: str) -> dict:
    return iv_surface(ticker)


@router.get("/strike-suggest")
async def strike_suggest(
    ticker: str,
    moneyness: float = Query(0.0, description="signed offset: -0.05 = 5% below spot"),
    kind: str = Query("call"),
    expiration: Optional[str] = None,
    spot_hint: Optional[float] = Query(None),
) -> dict:
    """Chain-aware strike picker — finds the closest listed strike to
    ``spot × (1 + moneyness)`` and reports the source so callers know whether
    they got a real chain strike or the snap_strike fallback."""
    chain = fetch_chain(ticker, expiration=expiration, spot_hint=spot_hint)
    spot = chain.spot or (spot_hint or 0.0)
    target = spot * (1 + moneyness) if spot > 0 else 0
    strike, source = nearest_available_strike(
        ticker, target=target, kind=kind, expiration=expiration,
        spot_hint=spot_hint,
    )
    return {
        "ticker": ticker.upper(), "spot": spot, "moneyness": moneyness,
        "target_strike": round(target, 2), "selected_strike": strike,
        "kind": kind, "expiration": expiration, "source": source,
    }


@router.get("/greeks")
async def greeks(
    spot: float = Query(..., gt=0),
    strike: float = Query(..., gt=0),
    dte: int = Query(..., ge=0),
    iv: float = Query(..., gt=0),
    kind: str = Query("call"),
    rate: Optional[float] = Query(None),
) -> dict:
    """Black-Scholes Greeks for a single contract. Used by the UI when a
    user hovers a chain row."""
    T = max(dte / 365.0, 1e-9)
    g = compute_greeks(spot, strike, T, iv, r=rate, kind=kind)
    return {"spot": spot, "strike": strike, "dte": dte, "iv": iv,
             "kind": kind, "greeks": g.to_dict()}


@router.get("/implied-vol")
async def implied_volatility(
    price: float = Query(..., gt=0),
    spot: float = Query(..., gt=0),
    strike: float = Query(..., gt=0),
    dte: int = Query(..., ge=0),
    kind: str = Query("call"),
    rate: Optional[float] = Query(None),
) -> dict:
    """Recover σ from a market price via bisection."""
    T = max(dte / 365.0, 1e-9)
    iv = implied_vol(price, spot, strike, T, r=rate, kind=kind)
    return {"price": price, "spot": spot, "strike": strike, "dte": dte,
             "kind": kind, "implied_vol": iv}


@router.get("/assignment-risk")
async def assignment_risk(
    spot: float = Query(..., gt=0),
    strike: float = Query(..., gt=0),
    dte: int = Query(..., ge=0),
    kind: str = Query("call"),
    ex_div_days: Optional[int] = None,
) -> dict:
    """Probability of early assignment on a SHORT contract (CSP / Covered Call)."""
    return assignment_probability(spot=spot, strike=strike, dte=dte, kind=kind,
                                     ex_div_days=ex_div_days, side="SHORT")
