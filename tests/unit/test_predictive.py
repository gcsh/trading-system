"""Predictive ML — training, predict fallback, and A/B blending in score_signal.

The model uses sklearn (lightgbm isn't installed). Tests synthesize labelled
DecisionLog-shaped rows so we can fit + predict end-to-end without touching
the live database.
"""
import os
import random
from dataclasses import dataclass

import pytest

from backend.bot.predictive import (
    MIN_TRAINING_ROWS,
    MLProbabilityModel,
    build_dataset,
    get_model,
    reset_model,
)


def _synthetic_rows(n: int, seed: int = 0):
    """Build labelled decision rows where 'win_probability' is the signal:
    high prob → wins, low prob → losses. The model should pick that up."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        prob = rng.uniform(0.3, 0.9)
        # noisy outcome: higher prob -> more likely positive PnL
        pnl = 50.0 if rng.random() < prob else -40.0
        rows.append({
            "ticker": "AAPL" if i % 2 == 0 else "MSFT",
            "action": "BUY_STOCK",
            "strategy": "trend",
            "confidence": prob,
            "status": "submitted",
            "regime_trend": "bullish" if i % 3 else "choppy",
            "regime_volatility": "normal",
            "regime_gamma": "long_gamma",
            "regime_label": "",
            "grade": "B",
            "win_probability": prob,
            "trade_id": i,
            "outcome_pnl": pnl,
            "outcome_status": "closed",
            "features_json": (
                '{"atr": 1.2, "rsi_14": 55.0, "iv_rank": 30, '
                '"composite_bias": 0.5, "pinning_probability": 0.1}'
            ),
        })
    return rows


def test_build_dataset_drops_rows_without_outcome():
    rows = [
        {"outcome_pnl": 10.0, "confidence": 0.6},
        {"outcome_pnl": None, "confidence": 0.7},     # dropped
        {"outcome_pnl": -5.0, "confidence": 0.5},
    ]
    X, y = build_dataset(rows)
    assert len(X) == 2
    assert y == [1, 0]


def test_predict_returns_none_when_no_model(tmp_path):
    reset_model()
    path = tmp_path / "missing.pkl"
    m = MLProbabilityModel(model_path=str(path))
    assert m.available is False
    assert m.predict({"confidence": 0.7}) is None


def test_train_fails_below_threshold(tmp_path):
    m = MLProbabilityModel(model_path=str(tmp_path / "m.pkl"))
    res = m.train(_synthetic_rows(MIN_TRAINING_ROWS - 1))
    assert res is None
    assert not os.path.exists(m.model_path)


def test_train_and_predict_roundtrip(tmp_path):
    reset_model()
    path = tmp_path / "model.pkl"
    m = MLProbabilityModel(model_path=str(path))
    rows = _synthetic_rows(80, seed=42)
    result = m.train(rows)
    assert result is not None
    assert result.rows == 80
    assert 0.0 <= result.base_rate <= 1.0
    assert os.path.exists(path)
    assert m.available

    # Higher input win-prob should map to a higher predicted probability.
    high = m.predict({"win_probability": 0.85, "confidence": 0.85,
                       "regime_trend": "bullish", "regime_volatility": "normal",
                       "regime_gamma": "long_gamma", "grade": "B"})
    low = m.predict({"win_probability": 0.30, "confidence": 0.30,
                      "regime_trend": "choppy", "regime_volatility": "normal",
                      "regime_gamma": "long_gamma", "grade": "B"})
    assert high is not None and low is not None
    assert 0.0 <= high <= 1.0 and 0.0 <= low <= 1.0
    assert high > low

    # A fresh model from the same path picks the artifact up.
    reset_model()
    other = MLProbabilityModel(model_path=str(path))
    assert other.available
    assert other.metadata()["rows"] == 80


def test_score_signal_falls_back_when_model_absent(monkeypatch, tmp_path):
    """ml_weight > 0 but no artifact → behaves exactly like ml_weight=0."""
    reset_model()
    monkeypatch.setenv("TB_ML_PROB_MODEL", str(tmp_path / "missing.pkl"))
    # Force a fresh singleton that reads the env var.
    from backend.bot import predictive as pmod
    pmod._MODEL = None

    from backend.bot.probability import score_signal
    from backend.bot.regime import MarketRegime
    from backend.bot.strategies.base import Action, Signal

    sig = Signal(ticker="AAPL", action=Action.BUY_STOCK, confidence=0.7,
                  reason="t", stop_loss=2.0, take_profit=4.0)
    regime = MarketRegime(trend="bullish", volatility="normal", gamma="long_gamma")
    features = {"composite_bias": 0.5}

    a = score_signal(sig, features, regime)
    b = score_signal(sig, features, regime, ml_weight=0.5)
    assert a.probability == b.probability   # no model -> no change
    assert b.components["ml_probability"] is None


def test_score_signal_blends_when_model_available(tmp_path):
    """When the singleton has a fitted model, score_signal should blend."""
    reset_model()
    path = tmp_path / "model.pkl"

    @dataclass
    class _StubModel:
        model_path: str
        available: bool = True
        def predict(self, _features):
            return 0.99      # extreme so blending is detectable
        def metadata(self):
            return {"rows": 100}

    # Inject the stub directly into the singleton slot.
    from backend.bot import predictive as pmod
    pmod._MODEL = _StubModel(model_path=str(path))

    from backend.bot.probability import score_signal
    from backend.bot.regime import MarketRegime
    from backend.bot.strategies.base import Action, Signal

    sig = Signal(ticker="AAPL", action=Action.BUY_STOCK, confidence=0.6,
                  reason="t", stop_loss=2.0, take_profit=4.0)
    regime = MarketRegime(trend="bullish", volatility="normal", gamma="long_gamma")
    features = {"composite_bias": 0.3}

    baseline = score_signal(sig, features, regime).probability
    blended = score_signal(sig, features, regime, ml_weight=0.5).probability
    assert blended > baseline                          # 0.99 stub pulls it up
    assert blended <= 0.95                             # respects prob_ceiling

    reset_model()


def test_predictive_status_endpoint_no_model(tmp_path, monkeypatch):
    """/predictive/status reports the no-model state without crashing."""
    monkeypatch.setenv("TB_ML_PROB_MODEL", str(tmp_path / "missing.pkl"))
    reset_model()
    from backend.bot import predictive as pmod
    pmod._MODEL = None

    from fastapi.testclient import TestClient

    from backend.main import app

    client = TestClient(app)
    body = client.get("/predictive/status").json()
    assert body["available"] is False
    assert body["artifact_exists"] is False
    assert body["min_training_rows"] == MIN_TRAINING_ROWS
