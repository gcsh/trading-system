"""Stage-11.6 Scenario endpoints.

  • ``GET  /scenarios/presets``        — list canonical stress scenarios
  • ``POST /scenarios/run``            — run a custom shock against live positions
  • ``GET  /scenarios/run/{preset}``   — run a named preset
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.bot.scenarios import (
    PRESETS,
    Shock,
    fetch_live_positions,
    preset_list,
    run_scenario,
)

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


@router.get("/presets")
async def presets() -> dict:
    return {"presets": preset_list()}


class ShockBody(BaseModel):
    spy_pct: float = 0.0
    vix_delta: float = 0.0
    rates_bps: float = 0.0
    sector_pcts: Optional[Dict[str, float]] = None
    label: Optional[str] = None
    # Optional override — when omitted we use the live executor's positions.
    positions: Optional[List[Dict[str, Any]]] = None


@router.post("/run")
async def run(body: ShockBody) -> dict:
    shock = Shock(
        spy_pct=body.spy_pct, vix_delta=body.vix_delta,
        rates_bps=body.rates_bps,
        sector_pcts=body.sector_pcts or {},
        label=body.label or "",
    )
    positions = body.positions if body.positions is not None else fetch_live_positions()
    return run_scenario(positions, shock).to_dict()


@router.get("/run/{preset}")
async def run_preset(preset: str) -> dict:
    shock = PRESETS.get(preset)
    if shock is None:
        raise HTTPException(status_code=404,
                              detail=f"unknown preset '{preset}'; see /scenarios/presets")
    positions = fetch_live_positions()
    return run_scenario(positions, shock).to_dict()
