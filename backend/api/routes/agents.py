"""Stage-11.3 Multi-Agent endpoints.

  • ``GET /agents/list``                     — agent roster + roles
  • ``POST /agents/consensus/preview``       — run consensus on hypothetical ctx
  • ``GET /agents/consensus/{trade_id}``     — persisted consensus for a trade
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.bot.agents import list_agents, run_consensus
from backend.bot.agents.contract import SOURCE_CATEGORIES
from backend.bot.agents.scorecard import build_scorecard, vote_weights
from backend.config import TUNABLES
from backend.db import session_scope
from backend.models.trade import Trade

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/list")
async def agents_list() -> dict:
    return {"agents": list_agents()}


@router.get("/contract")
async def agents_contract() -> dict:
    """Stage-20c — expose the current master contract knobs so the
    Settings UI can show them. All three are env-only on purpose;
    flipping them requires a backend restart. The endpoint is
    read-only — no write path here."""
    return {
        "chairman_authoritative": bool(TUNABLES.chairman_authoritative),
        "chairman_authoritative_env": "TB_CHAIRMAN_AUTHORITATIVE",
        "min_confidence_for_contribution": float(TUNABLES.min_confidence_for_contribution),
        "min_confidence_env": "TB_AGENTS_MIN_CONFIDENCE_FOR_CONTRIBUTION",
        "agent_quorum_min": int(TUNABLES.agent_quorum_min),
        "agent_quorum_env": "TB_AGENT_QUORUM_MIN",
        "source_categories": list(SOURCE_CATEGORIES),
    }


class ConsensusBody(BaseModel):
    ticker: str
    action: str = "BUY_STOCK"
    strategy: Optional[str] = None
    analytics: Optional[Dict[str, Any]] = None
    features: Optional[Dict[str, Any]] = None
    snapshot: Optional[Dict[str, Any]] = None
    portfolio_risk: Optional[Dict[str, Any]] = None
    optimizer: Optional[Dict[str, Any]] = None
    cross_asset: Optional[Dict[str, Any]] = None
    cohort: Optional[Dict[str, Any]] = None
    abstain_threshold: Optional[float] = None
    disagreement_threshold: Optional[float] = None


@router.post("/consensus/preview")
async def preview(body: ConsensusBody) -> dict:
    """Score a hypothetical decision context and return votes + consensus."""
    kwargs: Dict[str, Any] = {}
    if body.abstain_threshold is not None:
        kwargs["abstain_threshold"] = float(body.abstain_threshold)
    if body.disagreement_threshold is not None:
        kwargs["disagreement_threshold"] = float(body.disagreement_threshold)
    context = body.model_dump(
        exclude={"abstain_threshold", "disagreement_threshold"}
    )
    consensus = run_consensus(context, **kwargs)
    return {"consensus": consensus.to_dict()}


@router.get("/scorecard")
async def scorecard(limit: int = 2000) -> dict:
    """Per-agent accuracy over recent closed trades that carry a
    persisted consensus block. Foundation for dynamic vote-weighting."""
    return build_scorecard(limit=limit).to_dict()


@router.get("/weights")
async def weights() -> dict:
    """Hit-rate-derived per-agent vote weights. Agents with insufficient
    evidence get the default weight (1.0). Not yet wired into the
    consensus engine — opt-in by callers."""
    return {"weights": vote_weights()}


@router.get("/consensus/{trade_id}")
async def get_consensus(trade_id: int) -> dict:
    with session_scope() as session:
        trade = session.get(Trade, trade_id)
        if trade is None:
            raise HTTPException(status_code=404, detail="trade not found")
        detail_raw = trade.detail_json
        ticker = trade.ticker
        action = trade.action
    try:
        detail = json.loads(detail_raw or "{}") or {}
    except Exception:
        detail = {}
    consensus = detail.get("consensus")
    if not consensus:
        raise HTTPException(
            status_code=404,
            detail=("no consensus persisted (pre-Stage-11.3 row). Use "
                      "POST /agents/consensus/preview with the trade's context"),
        )
    return {"trade_id": trade_id, "ticker": ticker, "action": action,
             "consensus": consensus}
