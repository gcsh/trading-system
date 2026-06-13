"""Analytics inspection endpoint.

Returns the analytical-layer context for a ticker — the regime label, the unified
feature vector, the multi-timeframe confluence — plus hypothetical LONG/SHORT
grades so a user can see at a glance how the ranker views each direction.
Per-trade probability/rank are also attached to engine events as they fire.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from backend.bot.analytics import AnalyticsEngine
from backend.bot.confluence import confluence_for
from backend.bot.features import build_features
from backend.bot.regime import detect_regime
from backend.bot.strategies.base import Action, Signal

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _hypothetical(ticker: str, action: Action, snapshot) -> Signal:
    """A neutral, high-confidence signal used only to project hypothetical grades."""
    return Signal(
        action=action, ticker=ticker, confidence=0.7,
        reason="hypothetical for analytics view",
        strategy="analytics_probe", stop_loss=2.0, take_profit=4.0,
        metadata={"hypothetical": True},
    )


@router.get("/{ticker}")
async def analytics_for(request: Request, ticker: str) -> dict:
    """Regime, features, confluence + hypothetical LONG/SHORT grades."""
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="engine not available")
    ticker = ticker.upper()
    try:
        snap = engine.market_data.snapshot(ticker)
        data = snap.data
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"snapshot failed: {exc}") from exc

    regime = detect_regime(data)
    features = build_features(data)
    confluence = None
    try:
        confluence = confluence_for(ticker)
    except Exception:
        confluence = None

    # Honor the same predictive-ML A/B weight the engine uses so the analytics
    # view reflects what the bot would actually see during a cycle.
    ml_weight = 0.0
    try:
        from backend.db import session_scope
        from backend.models.config import load_config

        with session_scope() as session:
            pcfg = (load_config(session).get("predictive") or {})
        if pcfg.get("enabled"):
            ml_weight = float(pcfg.get("weight", 0.0) or 0.0)
    except Exception:
        ml_weight = 0.0

    eng = AnalyticsEngine()
    long_view = eng.evaluate(ticker, data, _hypothetical(ticker, Action.BUY_STOCK, data),
                              confluence=confluence, ml_weight=ml_weight)
    short_view = eng.evaluate(ticker, data, _hypothetical(ticker, Action.SELL_STOCK, data),
                               confluence=confluence, ml_weight=ml_weight)

    return {
        "ticker": ticker,
        "regime": regime.to_dict(),
        "features": features,
        "confluence": confluence.to_dict() if confluence else None,
        "hypothetical": {
            "long":  {"probability": long_view.probability.to_dict(),  "rank": long_view.rank.to_dict()},
            "short": {"probability": short_view.probability.to_dict(), "rank": short_view.rank.to_dict()},
        },
    }
