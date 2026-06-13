"""Stage-8 endpoints — stress scenarios, replay, canary, kill-switch."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.bot.canary import (
    get_state,
    halt,
    kill_switch_status,
    promote,
    rollback,
    set_kill_switch,
)
from backend.bot.replay import replay_session
from backend.bot.stress import (
    SCENARIOS,
    apply_scenario,
    available_scenarios,
    run_suite,
)


# ── stress ────────────────────────────────────────────────────────────────


stress_router = APIRouter(prefix="/stress", tags=["stress"])


@stress_router.get("/scenarios")
async def list_scenarios() -> dict:
    return {"scenarios": available_scenarios()}


class ApplyBody(BaseModel):
    snapshot: Dict[str, Any]
    scenarios: Optional[List[str]] = None


@stress_router.post("/apply")
async def apply(body: ApplyBody) -> dict:
    """Apply every requested scenario (default: all) to the supplied baseline
    snapshot. Returns mutated snapshots + expected behaviours."""
    results = run_suite(body.snapshot, scenarios=body.scenarios)
    return {"results": results, "count": len(results)}


@stress_router.post("/scenario/{name}")
async def scenario_apply(name: str, snapshot: Dict[str, Any]) -> dict:
    result = apply_scenario(name, snapshot)
    if result is None:
        raise HTTPException(status_code=404, detail=f"unknown scenario '{name}'")
    return result.to_dict()


# ── replay ────────────────────────────────────────────────────────────────


replay_router = APIRouter(prefix="/replay", tags=["replay"])


@replay_router.get("/{ticker}")
async def replay(ticker: str,
                   strategy: str = Query("adaptive"),
                   period: str = Query("1mo"),
                   interval: str = Query("1d"),
                   limit_events: int = Query(200, ge=10, le=2000)) -> dict:
    report = replay_session(strategy_name=strategy, ticker=ticker,
                              period=period, interval=interval,
                              limit_events=limit_events)
    return report.to_dict()


# ── canary state machine ─────────────────────────────────────────────────


canary_router = APIRouter(prefix="/canary", tags=["canary"])


@canary_router.get("/state")
async def canary_state() -> dict:
    return get_state().to_dict()


class PromoteBody(BaseModel):
    target: str
    capital: float = 500.0
    force: bool = False


@canary_router.post("/promote")
async def post_promote(body: PromoteBody) -> dict:
    """Move canary state to ``target``. Refuses unless gates pass OR force=true.
    Returns 422 on refusal so the UI can show why."""
    from backend.bot.gates import evaluate_gates
    from backend.api.routes.metrics import build_summary

    summary = build_summary()
    gates_summary = evaluate_gates(summary)
    result = promote(target=body.target, capital=body.capital,
                       gates_summary=gates_summary, force=body.force)
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result)
    return result


class RollbackBody(BaseModel):
    reason: str


@canary_router.post("/rollback")
async def post_rollback(body: RollbackBody) -> dict:
    return rollback(reason=body.reason)


@canary_router.post("/halt")
async def post_halt(body: RollbackBody) -> dict:
    return halt(reason=body.reason)


# ── kill switch ─────────────────────────────────────────────────────────


@canary_router.get("/kill-switch")
async def get_kill_switch() -> dict:
    return kill_switch_status()


class KillSwitchBody(BaseModel):
    active: bool
    reason: str = ""


@canary_router.post("/kill-switch")
async def set_kill_switch_endpoint(body: KillSwitchBody) -> dict:
    return set_kill_switch(body.active, reason=body.reason)
