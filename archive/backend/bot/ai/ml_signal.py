"""LightGBM-based quantitative signal.

Features come from the existing :class:`MarketSnapshot` (RSI, MACD, MA ratios,
volume, ADX, VIX, etc). The model predicts probability of an up-move on the
next bar. Output is converted to a :class:`Signal` with confidence proportional
to how far the probability sits from 0.5.

Designed so the rest of the system runs even when the model file is missing —
this is the typical state until the user runs the training script.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.bot.strategies.base import Action, Signal
from backend.config import SETTINGS

logger = logging.getLogger(__name__)

# Order matters — must match the feature pipeline in ``train.py``.
FEATURE_NAMES: List[str] = [
    "rsi",
    "macd",
    "macd_signal",
    "macd_hist",
    "ma50_ratio",          # price / ma50
    "ma200_ratio",         # price / ma200
    "volume_ratio",        # volume / avg_volume
    "iv_rank",
    "adx",
    "vix",
    "news_score",
    "pe_ratio",
    "earnings_days",
    "gap_pct",
    "unrealized_gain_pct",
    "rsi_5m",
    "momentum_5m",
    "range_3w_pct",
]


@dataclass
class MLPrediction:
    probability_up: float
    direction: str  # "up" | "down" | "neutral"
    confidence: float


def extract_features(snapshot: Dict[str, Any]) -> List[float]:
    """Build the feature row in canonical order.

    Missing fields default to neutral values (0 for ratios, 50 for RSI) so
    inference never blows up on partial snapshots.
    """
    price = float(snapshot.get("price", 0) or 0)
    ma50 = float(snapshot.get("ma50", 0) or 0) or 1.0
    ma200 = float(snapshot.get("ma200", 0) or 0) or 1.0
    volume = float(snapshot.get("volume", 0) or 0)
    avg_volume = float(snapshot.get("avg_volume", 1) or 1)
    return [
        float(snapshot.get("rsi", 50)),
        float(snapshot.get("macd", 0)),
        float(snapshot.get("macd_signal", 0)),
        float(snapshot.get("macd_hist", 0)),
        price / ma50 if ma50 else 1.0,
        price / ma200 if ma200 else 1.0,
        volume / avg_volume if avg_volume else 1.0,
        float(snapshot.get("iv_rank", 30)),
        float(snapshot.get("adx", 20)),
        float(snapshot.get("vix", 18)),
        float(snapshot.get("news_score", 0)),
        float(snapshot.get("pe_ratio", 20)),
        float(snapshot.get("earnings_days", 30)),
        float(snapshot.get("gap_pct", 0)),
        float(snapshot.get("unrealized_gain_pct", 0)),
        float(snapshot.get("rsi_5m", 50)),
        float(snapshot.get("momentum_5m", 0)),
        float(snapshot.get("range_3w_pct", 0.05)),
    ]


class MLSignalModel:
    """Lazy-loads a LightGBM booster from disk.

    Gracefully reports unavailable state when the model file isn't present —
    callers check ``available`` before trusting predictions.
    """

    def __init__(self, model_path: Optional[str] = None) -> None:
        self.model_path = model_path or SETTINGS.ml_model_path
        self._booster = None
        self._load_error: Optional[str] = None

    @property
    def available(self) -> bool:
        if self._booster is not None:
            return True
        if not os.path.exists(self.model_path):
            return False
        return self._load()

    def _load(self) -> bool:
        try:
            import lightgbm as lgb  # type: ignore

            self._booster = lgb.Booster(model_file=self.model_path)
            return True
        except Exception as exc:
            logger.warning("failed to load ML model: %s", exc)
            self._load_error = str(exc)
            return False

    def predict(self, snapshot: Dict[str, Any]) -> Optional[MLPrediction]:
        if not self.available:
            return None
        features = extract_features(snapshot)
        try:
            prob_up = float(self._booster.predict([features])[0])
        except Exception:
            logger.exception("ML predict failed")
            return None
        prob_up = max(0.0, min(1.0, prob_up))
        if prob_up > 0.55:
            direction = "up"
        elif prob_up < 0.45:
            direction = "down"
        else:
            direction = "neutral"
        # Confidence is distance from 50/50, normalised.
        confidence = abs(prob_up - 0.5) * 2
        return MLPrediction(probability_up=prob_up, direction=direction, confidence=confidence)

    def analyze(self, ticker: str, snapshot: Dict[str, Any]) -> Signal:
        prediction = self.predict(snapshot)
        if prediction is None:
            return Signal.hold(ticker, "ml_model", "model unavailable")
        if prediction.direction == "neutral":
            return Signal.hold(ticker, "ml_model", "model neutral")
        action = Action.BUY_STOCK if prediction.direction == "up" else Action.SELL_STOCK
        return Signal(
            ticker=ticker,
            action=action,
            confidence=prediction.confidence,
            reason=f"ML p_up={prediction.probability_up:.2f}",
            strategy="ml_model",
            metadata={"source": "ml_model", "probability_up": prediction.probability_up},
        )
