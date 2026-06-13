"""MITS Phase 15.C — /strategy/matrix/{ticker} endpoint.

Builds the StrategyMatrix for a single ticker by:
  1. Fetching recent bars + running detectors over the live window.
  2. Building the consolidated ``RegimeVector``.
  3. Pulling analog returns for the most-prominent detector hit.
  4. Composing an ``iv_state`` dict from the IV regime classifier.
  5. Calling ``build_strategy_matrix`` and returning ``.to_dict()``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from backend.api.routes.analysis import (
    _fetch_bars, _fetch_bars_dataframe, _run_detectors_in_window,
    _resolve_window,
)
from backend.bot.analysis.strategy_matrix import build_strategy_matrix
from backend.bot.corpus.analog_retrieval import retrieve_analogs

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/strategy", tags=["strategy"])


def _pick_top_pattern(hits: List[Dict[str, Any]]) -> Optional[str]:
    """Pick the most recent detector hit's pattern as the analog query."""
    if not hits:
        return None
    latest = hits[-1]
    return str(latest.get("pattern") or "") or None


def _build_iv_state(ticker: str, snapshot: Dict[str, Any]
                       ) -> Dict[str, Any]:
    features = snapshot.get("features") or {}
    iv_rank = snapshot.get("iv_rank")
    if iv_rank is None:
        iv_rank = features.get("iv_rank")
    iv_regime_label: Optional[str] = None
    current_iv: Optional[float] = None
    try:
        from backend.bot.iv_regime import classify_ticker
        report = classify_ticker(ticker)
        iv_regime_label = report.regime
        current_iv = report.current_iv
    except Exception:
        logger.debug("iv_regime classify failed for %s",
                      ticker, exc_info=True)
    return {
        "iv_rank": iv_rank,
        "iv_regime": iv_regime_label,
        "current_iv": current_iv,
    }


@router.get("/matrix/{ticker}")
async def get_strategy_matrix(
    ticker: str,
    horizon: str = Query("5d", pattern="^(1d|5d|20d)$"),
    regime: str = Query("auto"),
) -> Dict[str, Any]:
    """Build a full StrategyMatrix for a ticker."""
    ticker_u = (ticker or "").upper().strip()
    if not ticker_u:
        raise HTTPException(status_code=400, detail="ticker required")

    window = "today"
    interval, since_dt = _resolve_window(window)
    bars = _fetch_bars(ticker_u, window, interval)
    df = _fetch_bars_dataframe(ticker_u, window, interval)
    observations = _run_detectors_in_window(ticker_u, df, since_dt)

    spot: Optional[float] = None
    if bars:
        try:
            spot = float(bars[-1].get("close"))
        except (TypeError, ValueError):
            spot = None

    from backend.bot.features import build_features
    from backend.bot.regime.vector import build_regime_vector
    snapshot: Dict[str, Any] = {"price": spot or 0.0}
    snapshot["features"] = build_features(snapshot) or {}
    rv = build_regime_vector(ticker=ticker_u, snapshot=snapshot)

    top_pattern = _pick_top_pattern(observations) or "na"
    analogs = retrieve_analogs(
        ticker=ticker_u, regime_vector=rv, pattern=top_pattern,
        horizon=horizon, k=50, sector_fallback=True,
    )

    iv_state = _build_iv_state(ticker_u, snapshot)

    matrix = build_strategy_matrix(
        ticker=ticker_u, regime_vector=rv,
        pattern_hits=observations, analogs=analogs,
        iv_state=iv_state,
    )
    return matrix.to_dict()


__all__ = ["router"]
