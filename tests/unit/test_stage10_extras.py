"""Stage-10 items 5 (ensemble), 6 (theme heat), 8 (event decay).

Pinned behavior:
  • Ensemble trains + predict_proba; base models DIFFER (not identical preds)
  • Stacker output is a valid probability matrix
  • Theme heat: < min_sample → no heat; hot → +; cold → -; multiplier clamped
  • theme_size_multiplier returns the MIN across themes (cold-cohort governs)
  • Decay bands: 0/15m → 0.0; 45m → 0.25; 90m → 0.50; 180m → 1.0
  • can_trade_with_decay respects pre-event hard hold AND post-event decay
"""
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.bot.cohort_matrix.theme_heat import (
    MAX_SWING,
    MIN_SAMPLE,
    _heat_from_cohort,
    compute_theme_heat,
    theme_size_multiplier,
)
from backend.bot.event_risk.decay import (
    _DECAY_BANDS,
    _band_for,
    can_trade_with_decay,
    decay_multiplier,
)
from backend.bot.ml.ensemble import EnsembleStacker, create_ensemble
from backend.bot.ml.models import create_model, supported_models


# ── ensemble ──────────────────────────────────────────────────────────────


def _synthetic_xy(n=80, seed=11):
    import random
    import pandas as pd
    from backend.bot.ml.feature_store import (
        CATEGORICAL_FEATURES, NUMERIC_FEATURES,
    )
    rng = random.Random(seed)
    rows, y = [], []
    for _ in range(n):
        prob = rng.uniform(0.3, 0.9)
        won = 1 if rng.random() < prob else 0
        row = {col: rng.gauss(0.5, 0.2) for col in NUMERIC_FEATURES}
        row["win_probability"] = prob
        row["confidence"] = prob
        for col in CATEGORICAL_FEATURES:
            row[col] = rng.choice(["bullish", "bearish", "normal", "B"])
        rows.append(row); y.append(won)
    return pd.DataFrame(rows), y


class TestEnsemble:
    def test_supported_includes_ensemble(self):
        assert "ensemble" in supported_models()

    def test_create_ensemble_factory(self):
        assert isinstance(create_ensemble(), EnsembleStacker)

    def test_train_and_predict(self):
        X, y = _synthetic_xy()
        ens = create_model("ensemble")
        ens.fit(X, y)
        probs = ens.predict_proba(X)
        assert probs.shape == (len(X), 2)
        # every row probability sums to ≈ 1
        for row in probs:
            assert abs(sum(row) - 1.0) < 1e-3

    def test_base_models_differ(self):
        """The two base models must produce DIFFERENT predictions or the
        stacker buys nothing."""
        X, y = _synthetic_xy(seed=21)
        ens = create_model("ensemble")
        ens.fit(X, y)
        pa = ens.model_a_.predict_proba(X)[:, 1]
        pb = ens.model_b_.predict_proba(X)[:, 1]
        # at least one prediction must differ meaningfully
        diffs = [abs(a - b) for a, b in zip(pa, pb)]
        assert max(diffs) > 0.05

    def test_classes_attribute_sklearn_compat(self):
        X, y = _synthetic_xy()
        ens = create_model("ensemble")
        ens.fit(X, y)
        assert ens.classes_ is not None

    def test_predict_before_fit_raises(self):
        with pytest.raises(RuntimeError):
            create_ensemble().predict_proba(_synthetic_xy()[0])


# ── theme heat ───────────────────────────────────────────────────────────


class TestThemeHeatMath:
    def test_neutral_returns_zero(self):
        h = _heat_from_cohort(wr=0.5, exp=0.0)
        assert h == 0.0

    def test_hot_returns_positive(self):
        h = _heat_from_cohort(wr=0.7, exp=20.0)
        assert h > 0
        assert h <= MAX_SWING

    def test_cold_returns_negative(self):
        h = _heat_from_cohort(wr=0.3, exp=-15.0)
        assert h < 0
        assert h >= -MAX_SWING

    def test_clamped_to_max_swing(self):
        h = _heat_from_cohort(wr=1.0, exp=1000.0)
        assert h <= MAX_SWING


class TestComputeThemeHeat:
    def test_empty_db(self, temp_db):
        assert compute_theme_heat() == []

    def test_below_min_sample_dropped(self, temp_db):
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            s.add(Trade(ticker="NVDA", action="BUY_STOCK", quantity=1,
                         price=100, strategy="t", signal_source="x",
                         confidence=0.7, paper=1, status="closed",
                         pnl=10.0, instrument="stock"))
        # 1 trade < MIN_SAMPLE (8) → no heats
        assert compute_theme_heat() == []

    def test_hot_theme_recorded(self, temp_db):
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            for i in range(MIN_SAMPLE + 2):
                s.add(Trade(ticker="NVDA", action="BUY_STOCK", quantity=1,
                             price=100, strategy="t", signal_source="x",
                             confidence=0.7, paper=1, status="closed",
                             pnl=15.0 if i % 5 else -3.0,  # 80% wins
                             instrument="stock"))
        heats = compute_theme_heat()
        # NVDA spans Mag7 + AI infrastructure + Semis
        names = {h.theme for h in heats}
        assert "Semis" in names or "AI infrastructure" in names
        assert all(h.size_multiplier >= 1.0 - MAX_SWING for h in heats)
        assert any(h.heat > 0 for h in heats)

    def test_theme_size_multiplier_cold_governs(self, temp_db):
        """When a ticker spans multiple themes and some are cold, the
        LOWEST (coldest) multiplier wins."""
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            # Mostly losses on NVDA → AI/Semis themes cold
            for i in range(MIN_SAMPLE + 2):
                s.add(Trade(ticker="NVDA", action="BUY_STOCK", quantity=1,
                             price=100, strategy="t", signal_source="x",
                             confidence=0.7, paper=1, status="closed",
                             pnl=-10.0, instrument="stock"))
        mult = theme_size_multiplier("NVDA")
        # cold cohort should yield multiplier < 1
        assert mult < 1.0

    def test_no_themes_returns_one(self, temp_db):
        assert theme_size_multiplier("UNKNOWNTICK") == 1.0


# ── event-risk decay ─────────────────────────────────────────────────────


class TestDecayBands:
    def test_band_in_full_hold(self):
        b = _band_for(15)
        assert b["multiplier"] == 0.0

    def test_band_quarter(self):
        b = _band_for(45)
        assert b["multiplier"] == 0.25

    def test_band_half(self):
        b = _band_for(90)
        assert b["multiplier"] == 0.50

    def test_band_normal(self):
        b = _band_for(150)
        assert b["multiplier"] == 1.0


class TestDecayMultiplier:
    def test_no_event_in_window_returns_one(self):
        # Pick a year/time we know has no event nearby
        result = decay_multiplier(now=datetime(2020, 1, 1, 12, 0))
        assert result.size_multiplier == 1.0
        assert not result.in_decay_window

    def test_inside_decay_window(self):
        # CPI May 2026-06-11 12:00 — 45 min after = quarter band
        # (CPI has no Powell follow-up, so it stays the most-recent event)
        now = datetime(2026, 6, 11, 12, 45)
        result = decay_multiplier(now=now)
        assert result.in_decay_window
        assert result.size_multiplier == 0.25
        assert "CPI" in (result.event_name or "")

    def test_resume_at_in_future(self):
        now = datetime(2026, 6, 11, 12, 45)
        result = decay_multiplier(now=now)
        assert result.suggested_resume_at is not None


class TestCanTradeWithDecay:
    def test_hard_hold_before_event(self):
        # 15 min before CPI May (12:00 on 6/11) — still in pre-event hard hold
        now = datetime(2026, 6, 11, 11, 45)
        out = can_trade_with_decay(ticker="NVDA", now=now)
        assert not out["can_trade"]
        assert out["size_multiplier"] == 0.0

    def test_decay_after_event(self):
        # 45 min after CPI May → quarter size
        now = datetime(2026, 6, 11, 12, 45)
        out = can_trade_with_decay(ticker="NVDA", now=now)
        assert out["can_trade"]
        assert out["size_multiplier"] == 0.25
        assert out["decay"] is not None

    def test_full_size_outside_event_window(self):
        now = datetime(2026, 6, 25, 14, 30)
        out = can_trade_with_decay(ticker="NVDA", now=now)
        assert out["can_trade"]
        assert out["size_multiplier"] == 1.0
        assert out["decay"] is None


# ── live API integration ──────────────────────────────────────────────


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class TestEndpoints:
    def test_theme_heat_empty(self, client):
        body = client.get("/cohorts/theme-heat").json()
        assert body["heats"] == []

    def test_theme_heat_ticker(self, client):
        body = client.get("/cohorts/theme-heat/NVDA").json()
        assert body["ticker"] == "NVDA"
        assert body["size_multiplier"] == 1.0      # no data → neutral

    def test_decay_now_endpoint(self, client):
        body = client.get("/event-risk/decay").json()
        assert "in_decay_window" in body
        assert "size_multiplier" in body

    def test_decay_ticker_endpoint(self, client):
        body = client.get("/event-risk/decay/NVDA").json()
        assert "can_trade" in body
        assert "size_multiplier" in body

    def test_ml_train_ensemble_lifecycle(self, client):
        # Seed enough labelled rows so /ml/train accepts
        import json
        import random
        from backend.db import session_scope
        from backend.models.decision_log import DecisionLog
        rng = random.Random(0)
        with session_scope() as s:
            for i in range(60):
                prob = rng.uniform(0.3, 0.9)
                pnl = 25.0 if rng.random() < prob else -18.0
                s.add(DecisionLog(
                    ticker="NVDA", action="BUY_STOCK", strategy="t",
                    confidence=prob, status="submitted",
                    regime_trend="bullish" if i % 3 else "choppy",
                    regime_volatility="normal", regime_gamma="long_gamma",
                    grade="B", win_probability=prob, trade_id=200_000 + i,
                    outcome_pnl=pnl, outcome_status="closed",
                    features_json=json.dumps({"atr": 1.2, "rsi_14": 55,
                                                "iv_rank": 30,
                                                "composite_bias": 0.5}),
                ))
        r = client.post("/ml/train", json={
            "model_type": "ensemble", "calibration": "isotonic",
            "set_active": False, "notes": "stage-10b ensemble test",
        })
        assert r.status_code == 200, r.json()
        body = r.json()
        assert "ensemble" in body["model"]["version"]
        assert body["model"]["rows_trained"] == 60
