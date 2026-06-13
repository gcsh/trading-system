"""Stage-15 — per-regime feature importance.

Pinned:
  • Cold start (no model) → uniform fallback per regime
  • With enough labelled rows per regime → permutation importance per bucket
  • Sparse regime → uniform fallback for that bucket only
  • Caching works (second call returns the same object)
  • Endpoint returns dict keyed by regime
"""
import json
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.bot.explain import (
    ImportanceReport,
    compute_importance_by_regime,
    reset_cache,
    reset_regime_cache,
)


@pytest.fixture(autouse=True)
def _isolate():
    reset_cache()
    reset_regime_cache()
    yield
    reset_cache()
    reset_regime_cache()


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


def _seed_decisions(*, n_per_regime=40):
    """Seed labelled DecisionLog rows for two regimes."""
    from backend.db import session_scope
    from backend.models.decision_log import DecisionLog
    import random
    random.seed(11)
    with session_scope() as s:
        for regime in ("bullish", "bearish"):
            for i in range(n_per_regime):
                is_win = random.random() < (0.62 if regime == "bullish" else 0.42)
                sign = 1 if is_win else -1
                row = DecisionLog(
                    ticker="NVDA",
                    action="BUY_CALL" if regime == "bullish" else "BUY_PUT",
                    strategy="trend_pullback",
                    confidence=0.70 + 0.05 * sign,
                    status="submitted",
                    regime_trend=regime,
                    regime_volatility="normal",
                    regime_gamma="long_gamma" if regime == "bullish" else "short_gamma",
                    regime_label=f"{regime} test",
                    grade="A" if is_win else "C",
                    win_probability=0.60 + 0.10 * sign,
                    trade_id=hash((regime, i)) % 10_000,
                    outcome_pnl=80.0 if is_win else -60.0,
                    outcome_status="closed",
                    features_json=json.dumps({
                        "rsi_14": 65 + 5 * sign,
                        "iv_rank": 40 + 10 * sign,
                        "composite_bias": 0.3 * sign,
                        "darkpool_bias": 0.1 * sign,
                        "pinning_probability": 0.2,
                        "gex_total": 0.4 * sign,
                        "atr": 2.5,
                        "hedging_pressure": "normal",
                        "dominant_wall": "neutral",
                    }),
                )
                row.timestamp = datetime.utcnow() + timedelta(minutes=i)
                s.add(row)


def _train_model():
    from backend.bot.ml.feature_store import build_dataset
    from backend.bot.ml.models import create_model
    from backend.bot.ml.registry import register_model, set_active
    X, y, _ = build_dataset(min_closed=20)
    assert X is not None
    pipe = create_model("hist_gb")
    pipe.fit(X, y)
    reg = register_model(model=pipe, model_type="hist_gb",
                            calibration=None, rows_trained=len(y),
                            notes="regime-test")
    set_active(reg.version)
    return reg.version


class TestComputeByRegime:
    def test_cold_start_returns_uniform(self, temp_db):
        out = compute_importance_by_regime()
        assert "bullish" in out and "bearish" in out
        assert all(rpt.method == "uniform_fallback" for rpt in out.values())

    def test_with_data_returns_permutation(self, temp_db):
        _seed_decisions(n_per_regime=40)
        _train_model()
        out = compute_importance_by_regime(min_per_regime=20)
        # Both regimes should have computed importance
        assert isinstance(out["bullish"], ImportanceReport)
        assert isinstance(out["bearish"], ImportanceReport)
        # At least one should be method=permutation
        methods = {rpt.method for rpt in out.values()}
        assert "permutation" in methods

    def test_caching(self, temp_db):
        _seed_decisions(n_per_regime=40)
        _train_model()
        r1 = compute_importance_by_regime(min_per_regime=20)
        r2 = compute_importance_by_regime(min_per_regime=20)
        # Same object back via cache
        assert r1["bullish"] is r2["bullish"]

    def test_sparse_regime_falls_through(self, temp_db):
        _seed_decisions(n_per_regime=40)
        _train_model()
        out = compute_importance_by_regime(min_per_regime=100)
        # Both regimes have only 40 → both fall to uniform
        assert all(rpt.method == "uniform_fallback" for rpt in out.values())


class TestEndpoint:
    def test_endpoint_returns_dict_keyed_by_regime(self, client):
        body = client.get("/explain/importance/by-regime").json()
        assert isinstance(body, dict)
        # Cold start → at least the canonical regimes appear
        assert "bullish" in body or "unknown" in body

    def test_top_k_clamp(self, client):
        body = client.get("/explain/importance/by-regime?top_k=3").json()
        for rpt in body.values():
            assert len(rpt["importances"]) <= 3
