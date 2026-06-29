"""Probabilistic Signal Scoring Engine.

Turns a discrete BUY/SELL/HOLD signal into a probabilistic view: how likely is
this setup to work, how big a move do we expect, what's the risk/reward, what's
the time horizon, and how confident are we in those numbers?

This first cut is a transparent, calibrated heuristic — it starts from the
strategy's own confidence and shifts it by how well the regime, multi-timeframe
confluence, and live options flow corroborate the direction. A trained model
(XGBoost/LightGBM) can later replace ``_combine`` without changing the interface.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from backend.bot.strategies.base import Action, Signal
from backend.config import TUNABLES

_BUY_ACTIONS = {Action.BUY_STOCK, Action.BUY_CALL, Action.BULL_CALL_SPREAD, Action.BUY_STRADDLE}
_SELL_ACTIONS = {Action.SELL_STOCK, Action.BUY_PUT, Action.SELL_COVERED_CALL, Action.SELL_CSP}


@dataclass
class SignalProbability:
    direction: str = "neutral"      # LONG | SHORT | NEUTRAL
    probability: float = 0.0        # 0-1 — calibrated win probability
    expected_move: Optional[float] = None    # in % of price
    risk_reward: Optional[float] = None      # take_profit_pct / stop_loss_pct
    time_horizon: str = "swing"     # intraday | swing | position
    confidence: float = 0.0         # 0-1 — confidence in the probability itself
    components: Dict[str, float] = None      # transparency: each input's contribution

    def to_dict(self) -> dict:
        return asdict(self)


def _direction(signal: Signal) -> str:
    if signal.action in _BUY_ACTIONS:
        return "LONG"
    if signal.action in _SELL_ACTIONS:
        return "SHORT"
    return "NEUTRAL"


def _aligned(direction: str, bias: float) -> float:
    """+bias for a LONG when bias>0, +|bias| for a SHORT when bias<0, else - against."""
    if direction == "LONG":
        return bias
    if direction == "SHORT":
        return -bias
    return 0.0


def _horizon(signal: Signal) -> str:
    meta = signal.metadata or {}
    raw = str(meta.get("time_exit") or meta.get("horizon") or "").lower()
    if raw and ("min" in raw or ":" in raw or "intraday" in raw):
        return "intraday"
    if signal.dte and signal.dte <= 5:
        return "intraday"
    if signal.dte and signal.dte >= 60:
        return "position"
    return "swing"


def _expected_move(features: Dict[str, Any]) -> Optional[float]:
    # Prefer ATR as % of price; else a 1-day implied-move proxy from IV.
    atr = features.get("atr")
    rsi = features.get("rsi_14")   # nudge — handy as a heuristic
    iv = features.get("iv_rank")
    if isinstance(atr, (int, float)) and atr > 0:
        return round(float(atr), 4)   # already in price units; UI can ratio
    if isinstance(iv, (int, float)) and iv > 0:
        # iv_rank is 0-100; rough 1-day move ≈ iv_rank/100 * sqrt(1/252) * 100%
        return round(iv / 100.0 * (1 / 252) ** 0.5 * 100, 3)
    _ = rsi
    return None


def score_signal(
    signal: Signal,
    features: Dict[str, Any],
    regime: Any,
    confluence: Optional[Any] = None,
    ml_weight: float = 0.0,
) -> SignalProbability:
    """Score a signal into a calibrated win probability + R/R + horizon."""
    direction = _direction(signal)
    if direction == "NEUTRAL":
        return SignalProbability(direction="NEUTRAL", probability=0.0, confidence=0.0,
                                  components={})

    base = max(0.0, min(1.0, float(signal.confidence or 0.0)))

    # Regime trend bias: +1 bullish, -1 bearish, 0 choppy/unknown.
    trend = getattr(regime, "trend", "unknown")
    regime_bias = 1.0 if trend == "bullish" else (-1.0 if trend == "bearish" else 0.0)
    regime_adj = 0.10 * _aligned(direction, regime_bias)
    # High volatility shaves a touch of confidence (whippy environment).
    if getattr(regime, "volatility", "normal") == "high":
        regime_adj -= 0.04

    # Multi-timeframe confluence — strong agreement adds, conflicts subtract.
    conf_adj = 0.0
    conf_score = float(getattr(confluence, "score", 0.0) or 0.0) if confluence else 0.0
    conf_dir = getattr(confluence, "direction", None) if confluence else None
    if confluence and conf_dir in ("bullish", "bearish"):
        bias = 1.0 if conf_dir == "bullish" else -1.0
        conf_adj = 0.15 * _aligned(direction, bias) * conf_score

    # Composite directional bias from features (technicals + flow + news).
    cbias = float(features.get("composite_bias") or 0.0)
    feat_adj = 0.10 * _aligned(direction, cbias)

    # Dark-pool corroboration (institutional confirm).
    dark_adj = 0.04 if features.get("darkpool_bias") and direction == "LONG" else 0.0

    probability = base + regime_adj + conf_adj + feat_adj + dark_adj
    probability = max(TUNABLES.prob_floor, min(TUNABLES.prob_ceiling, probability))

    # Optional A/B blend with the trained ML probability. ``ml_weight`` 0 means
    # heuristic-only (status quo); anything > 0 blends iff the model speaks.
    ml_prob = None
    ml_adj = 0.0
    if ml_weight and ml_weight > 0:
        try:
            from backend.bot.predictive import get_model

            ml_input = dict(features)
            ml_input.update({
                "regime_trend": getattr(regime, "trend", "unknown"),
                "regime_volatility": getattr(regime, "volatility", "normal"),
                "regime_gamma": getattr(regime, "gamma", "unknown"),
                "confidence": base,
                "win_probability": probability,
            })
            ml_prob = get_model().predict(ml_input)
        except Exception:
            ml_prob = None
        if ml_prob is not None:
            w = max(0.0, min(1.0, float(ml_weight)))
            blended = (1.0 - w) * probability + w * ml_prob
            ml_adj = blended - probability
            probability = max(TUNABLES.prob_floor, min(TUNABLES.prob_ceiling, blended))

    # Risk / reward from the strategy's own stop & target (percent).
    sl = float(signal.stop_loss or 0.0)
    tp = float(signal.take_profit or 0.0)
    risk_reward = round(tp / sl, 2) if sl > 0 and tp > 0 else None

    # Confidence in the probability = how many corroborating axes we used.
    used = sum(1 for x in (regime_bias, conf_score, cbias, features.get("darkpool_bias")) if x)
    confidence = round(min(1.0, 0.45 + 0.12 * used), 2)

    return SignalProbability(
        direction=direction,
        probability=round(probability, 4),
        expected_move=_expected_move(features),
        risk_reward=risk_reward,
        time_horizon=_horizon(signal),
        confidence=confidence,
        components={
            "base_signal": round(base, 3),
            "regime_adj": round(regime_adj, 3),
            "confluence_adj": round(conf_adj, 3),
            "features_adj": round(feat_adj, 3),
            "darkpool_adj": round(dark_adj, 3),
            "ml_adj": round(ml_adj, 3),
            "ml_probability": round(ml_prob, 3) if ml_prob is not None else None,
        },
    )
