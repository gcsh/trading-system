"""Stage-2 execution-cost endpoints.

Three surfaces:
  • ``GET /execution/costs/preview``  — cost estimate for a hypothetical order
  • ``GET /execution/brokers``         — broker catalog (constraints + comm sched)
  • ``GET /execution/brokers/{name}``  — one broker's full profile
  • ``POST /execution/validate-order`` — run constraint checks on a plan
  • ``POST /execution/simulate-fill``  — partial-fill walk over supplied bars
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.bot.broker_constraints import (
    BROKER_PROFILES,
    get_profile,
    validate_order,
)
from backend.bot.execution_costs import (
    COMMISSION_CATALOG,
    estimate_total_cost,
)
from backend.bot.execution_sim import simulate_fill, simulate_legs

router = APIRouter(prefix="/execution", tags=["execution"])


@router.get("/costs/preview")
async def cost_preview(
    broker: str = Query("local_paper"),
    instrument: str = Query("stock"),
    side: str = Query("BUY"),
    quantity: float = Query(1, gt=0),
    price: float = Query(100.0, gt=0),
    strike: Optional[float] = Query(None),
    atr: Optional[float] = Query(None),
    iv_rank: Optional[float] = Query(None),
    volume_avg: Optional[float] = Query(None),
) -> dict:
    """Cost estimate as the UI / strategy would consume before sending an order."""
    snapshot: Dict[str, Any] = {"price": price}
    if atr is not None: snapshot["atr"] = atr
    if iv_rank is not None: snapshot["iv_rank"] = iv_rank
    if volume_avg is not None: snapshot["volume_avg"] = volume_avg
    est = estimate_total_cost(broker=broker, instrument=instrument, side=side,
                                quantity=quantity, price=price,
                                snapshot=snapshot, strike=strike)
    return {"estimate": est.to_dict(),
             "broker": broker, "instrument": instrument, "side": side}


@router.get("/brokers")
async def list_brokers() -> dict:
    """Every broker in the catalog with constraints + commission schedule."""
    out = []
    for name, profile in BROKER_PROFILES.items():
        sched = COMMISSION_CATALOG.get(name)
        out.append({
            "name": name,
            "profile": profile.to_dict(),
            "commission": ({
                "stock_per_share": sched.stock_per_share,
                "stock_minimum": sched.stock_minimum,
                "stock_maximum_pct": sched.stock_maximum_pct,
                "option_per_contract": sched.option_per_contract,
                "option_minimum": sched.option_minimum,
            } if sched else None),
        })
    return {"brokers": out}


@router.get("/brokers/{name}")
async def broker_detail(name: str) -> dict:
    if name not in BROKER_PROFILES:
        raise HTTPException(status_code=404, detail=f"unknown broker '{name}'")
    profile = get_profile(name)
    sched = COMMISSION_CATALOG.get(name)
    return {
        "name": name,
        "profile": profile.to_dict(),
        "commission": ({
            "stock_per_share": sched.stock_per_share,
            "stock_minimum": sched.stock_minimum,
            "stock_maximum_pct": sched.stock_maximum_pct,
            "option_per_contract": sched.option_per_contract,
            "option_minimum": sched.option_minimum,
        } if sched else None),
    }


# ── POST bodies ─────────────────────────────────────────────────────────────


class ValidateOrderBody(BaseModel):
    plan: Dict[str, Any]
    broker: str = "local_paper"
    order_type: str = "market"


@router.post("/validate-order")
async def post_validate_order(body: ValidateOrderBody) -> dict:
    violations = validate_order(body.plan, broker=body.broker,
                                  order_type=body.order_type)
    return {"ok": not violations,
             "violations": [v.to_dict() for v in violations],
             "broker": body.broker}


class SimulateFillBody(BaseModel):
    side: str
    quantity: float
    bars: List[Dict[str, Any]]
    snapshot: Optional[Dict[str, Any]] = None
    instrument: str = "stock"
    volume_share_cap: Optional[float] = None
    max_bars: int = 10


@router.post("/simulate-fill")
async def post_simulate_fill(body: SimulateFillBody) -> dict:
    result = simulate_fill(side=body.side, quantity=body.quantity,
                            bars=body.bars, snapshot=body.snapshot,
                            instrument=body.instrument,
                            volume_share_cap=body.volume_share_cap,
                            max_bars=body.max_bars)
    return result.to_dict()


class SimulateLegsBody(BaseModel):
    legs: List[Dict[str, Any]]
    broker: str = "local_paper"
    leg_fail_prob: Optional[float] = None
    rng_seed: Optional[int] = None


@router.post("/simulate-legs")
async def post_simulate_legs(body: SimulateLegsBody) -> dict:
    profile = get_profile(body.broker)
    from backend.config import TUNABLES
    fail_prob = (body.leg_fail_prob if body.leg_fail_prob is not None
                  else float(getattr(TUNABLES, "leg_fail_prob_no_atomicity", 0.05)))
    result = simulate_legs(
        body.legs,
        atomicity_supported=profile.leg_atomicity_supported,
        leg_fail_prob=fail_prob,
        rng_seed=body.rng_seed,
    )
    return result.to_dict()
