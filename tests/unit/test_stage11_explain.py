"""Stage-11.7 Feature Importance + per-trade attribution.

Pinned:
  • Cold-start (no model trained) → uniform_fallback report renders
  • compute_importance + caching: second call returns cached object
  • Permutation method runs when enough labelled rows exist + active model
  • explain_trade_features 404 path + happy path
  • Quality tagging maps known feature bands correctly
  • Endpoints return well-formed JSON
"""
import json
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.bot.explain import (
    FeatureImportance,
    ImportanceReport,
    _quality_tag,
    compute_importance,
    explain_trade_features,
    reset_cache,
    top_features,
)


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    reset_cache()
    return TestClient(main_mod.app)


@pytest.fixture(autouse=True)
def _isolate_cache():
    """Each test starts with an empty importance cache."""
    reset_cache()
    yield
    reset_cache()


# ── quality tagging ─────────────────────────────────────────────────────


class TestQualityTag:
    def test_categorical_grade(self):
        assert _quality_tag("grade", "A") == "high"
        assert _quality_tag("grade", "A+") == "high"
        assert _quality_tag("grade", "F") == "low"
        assert _quality_tag("grade", "C") == "mid"

    def test_regime_trend(self):
        assert _quality_tag("regime_trend", "bullish") == "high"
        assert _quality_tag("regime_trend", "bearish") == "low"
        assert _quality_tag("regime_trend", "choppy") == "mid"

    def test_numeric_bands(self):
        assert _quality_tag("rsi_14", 80) == "high"
        assert _quality_tag("rsi_14", 25) == "low"
        assert _quality_tag("rsi_14", 50) == "mid"
        assert _quality_tag("iv_rank", 90) == "high"
        assert _quality_tag("win_probability", 0.72) == "high"
        assert _quality_tag("win_probability", 0.40) == "low"
        assert _quality_tag("composite_bias", 0.5) == "high"
        assert _quality_tag("composite_bias", -0.5) == "low"

    def test_none_and_unparseable(self):
        assert _quality_tag("rsi_14", None) == "n/a"
        assert _quality_tag("rsi_14", "abc") == "n/a"
        assert _quality_tag("regime_trend", "unknown") == "n/a"


# ── cold-start fallback ─────────────────────────────────────────────────


class TestColdStart:
    def test_uniform_fallback_when_no_model(self, temp_db):
        rpt = compute_importance()
        assert isinstance(rpt, ImportanceReport)
        assert rpt.method == "uniform_fallback"
        # All NUMERIC + CATEGORICAL features represented
        assert len(rpt.importances) == 15
        # Uniform weights all equal
        weights = {fi.importance for fi in rpt.importances}
        assert len(weights) == 1
        assert rpt.warnings


# ── permutation path ────────────────────────────────────────────────────


def _make_labelled_decisions(n=80, *, win_rate=0.6, trend_bias=0.4):
    """Seed n DecisionLog rows split 50/50 wins/losses with separable features
    so a HGB ranker can actually learn something."""
    from backend.db import session_scope
    from backend.models.decision_log import DecisionLog
    import random
    random.seed(7)
    with session_scope() as s:
        for i in range(n):
            is_win = random.random() < win_rate
            pnl = 100.0 if is_win else -80.0
            # Make win rows look bullish + high prob; loss rows the opposite.
            sign = 1 if is_win else -1
            row = DecisionLog(
                ticker="NVDA", action="BUY_CALL", strategy="trend_pullback",
                confidence=0.7 + 0.15 * sign, status="submitted",
                regime_trend=("bullish" if is_win else "bearish"),
                regime_volatility="normal", regime_gamma="long_gamma",
                regime_label="x", grade=("A" if is_win else "C"),
                win_probability=0.6 + 0.15 * sign + random.gauss(0, 0.03),
                trade_id=i + 1, outcome_pnl=pnl, outcome_status="closed",
                features_json=json.dumps({
                    "rsi_14": 65 + 5 * sign + random.gauss(0, 2),
                    "iv_rank": 50 + 10 * sign,
                    "composite_bias": trend_bias * sign + random.gauss(0, 0.05),
                    "darkpool_bias": 0.2 * sign,
                    "pinning_probability": 0.2,
                    "gex_total": 0.5 * sign,
                    "atr": 2.5,
                    "hedging_pressure": "normal",
                    "dominant_wall": "neutral",
                }),
            )
            row.timestamp = datetime.utcnow() + timedelta(minutes=i)
            s.add(row)


def _train_quick_model(temp_db):
    """Train a HistGradientBoosting model so compute_importance has an active
    model to introspect. Returns the version string."""
    from backend.bot.ml.feature_store import build_dataset
    from backend.bot.ml.models import create_model
    from backend.bot.ml.registry import register_model, set_active
    X, y, meta = build_dataset(min_closed=20)
    assert X is not None, f"not enough labelled data: {meta}"
    pipeline = create_model("hist_gb")
    pipeline.fit(X, y)
    registered = register_model(
        model=pipeline, model_type="hist_gb", calibration=None,
        rows_trained=len(y), notes="explain-test",
    )
    set_active(registered.version)
    return registered.version


class TestPermutationPath:
    def test_runs_with_real_model_and_data(self, temp_db):
        _make_labelled_decisions(n=90)
        _train_quick_model(temp_db)
        rpt = compute_importance()
        assert rpt.method == "permutation"
        assert rpt.sample_size > 0
        assert rpt.model_version
        # Top feature ideally the one we seeded as separable; at minimum
        # the importances are sorted descending.
        scores = [fi.importance for fi in rpt.importances]
        assert scores == sorted(scores, reverse=True)
        assert len(rpt.importances) >= 5

    def test_cached_on_second_call(self, temp_db):
        _make_labelled_decisions(n=90)
        _train_quick_model(temp_db)
        rpt1 = compute_importance()
        rpt2 = compute_importance()
        assert rpt1 is rpt2          # cache hit returns the same object
        rpt3 = compute_importance(force=True)
        assert rpt3 is not rpt2      # force recomputes

    def test_top_features_helper(self, temp_db):
        _make_labelled_decisions(n=90)
        _train_quick_model(temp_db)
        rows = top_features(k=5)
        assert len(rows) == 5
        assert all("feature" in r and "importance" in r for r in rows)


# ── per-trade attribution ───────────────────────────────────────────────


class TestPerTradeAttribution:
    def test_returns_none_for_unknown(self, temp_db):
        assert explain_trade_features(999999) is None

    def test_pulls_top_features_from_trade(self, temp_db):
        from backend.db import session_scope
        from backend.models.trade import Trade

        detail = {
            "snapshot": {"rsi": 72, "vix": 14, "iv_rank": 35},
            "analytics": {
                "regime": {"trend": "bullish", "volatility": "normal",
                              "gamma": "long_gamma"},
                "rank": {"grade": "A"},
                "probability": {"probability": 0.7, "direction": "LONG"},
                "features": {
                    "rsi_14": 72, "iv_rank": 35,
                    "composite_bias": 0.4, "pinning_probability": 0.2,
                    "darkpool_bias": 0.1, "gex_total": 0.3,
                    "hedging_pressure": "normal", "dominant_wall": "neutral",
                },
            },
        }
        with session_scope() as s:
            t = Trade(ticker="NVDA", action="BUY_CALL", quantity=1, price=215,
                       strategy="trend_pullback", signal_source="t",
                       confidence=0.7, paper=1, status="open",
                       instrument="option",
                       detail_json=json.dumps(detail))
            s.add(t); s.flush()
            tid = t.id

        out = explain_trade_features(tid, top_k=5)
        assert out["trade_id"] == tid
        assert out["ticker"] == "NVDA"
        assert out["action"] == "BUY_CALL"
        assert len(out["attributions"]) == 5
        # Each attribution has importance + value + quality
        for a in out["attributions"]:
            assert "feature" in a and "importance" in a
            assert "value" in a and "quality" in a


# ── endpoints ───────────────────────────────────────────────────────────


class TestExplainEndpoints:
    def test_importance_endpoint_cold_start(self, client):
        body = client.get("/explain/importance").json()
        assert "importances" in body
        # uniform_fallback in a fresh DB
        assert body["method"] in ("uniform_fallback", "permutation")
        assert "warnings" in body

    def test_importance_endpoint_top_k_clamp(self, client):
        body = client.get("/explain/importance?top_k=3").json()
        assert len(body["importances"]) == 3

    def test_trade_endpoint_404(self, client):
        assert client.get("/explain/features/999999").status_code == 404

    def test_trade_endpoint_200(self, client):
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            t = Trade(ticker="X", action="BUY_STOCK", quantity=1, price=100,
                       strategy="s", signal_source="t", confidence=0.7,
                       paper=1, status="open", instrument="stock",
                       detail_json=json.dumps({
                           "snapshot": {"rsi": 60, "vix": 18},
                           "analytics": {
                               "regime": {"trend": "bullish"},
                               "rank": {"grade": "B"},
                               "features": {"rsi_14": 60, "iv_rank": 40},
                           },
                       }))
            s.add(t); s.flush()
            tid = t.id
        body = client.get(f"/explain/features/{tid}").json()
        assert body["trade_id"] == tid
        assert len(body["attributions"]) >= 1
