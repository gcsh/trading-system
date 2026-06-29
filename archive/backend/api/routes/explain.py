"""Stage-11.7 Feature Importance + per-trade attribution endpoints.

Lives under the ``/explain`` namespace alongside Stage-7's
``/explain/trade/{id}`` (decision-context surface). Sub-paths kept distinct:

  • ``GET  /explain/importance``         — global feature importance for the active model
  • ``GET  /explain/features/{trade_id}`` — per-trade feature attribution
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from backend.bot.explain import (
    compute_importance,
    compute_importance_by_regime,
    explain_trade_features,
)

router = APIRouter(prefix="/explain", tags=["explain"])


@router.get("/importance")
async def importance(force: bool = Query(False),
                        top_k: int = Query(20, ge=1, le=50)) -> dict:
    rpt = compute_importance(force=force).to_dict()
    rpt["importances"] = rpt["importances"][:top_k]
    return rpt


@router.get("/importance/by-regime")
async def importance_by_regime(force: bool = Query(False),
                                  top_k: int = Query(15, ge=1, le=50),
                                  min_per_regime: int = Query(30, ge=10, le=500),
                                  ) -> dict:
    """Stage-15 — permutation importance split per regime_trend.

    Answers "in bullish tape feature X matters; in chop feature Y does".
    Regimes with fewer than ``min_per_regime`` labelled rows fall back to
    uniform weights so the UI always renders something.
    """
    reports = compute_importance_by_regime(force=force,
                                              min_per_regime=min_per_regime)
    return {
        regime: {
            **rpt.to_dict(),
            "importances": rpt.to_dict()["importances"][:top_k],
        }
        for regime, rpt in reports.items()
    }


@router.get("/features/{trade_id}")
async def features(trade_id: int, top_k: int = Query(5, ge=1, le=15)) -> dict:
    payload = explain_trade_features(trade_id, top_k=top_k)
    if payload is None:
        raise HTTPException(status_code=404, detail="trade not found")
    return payload
