"""MITS Phase 3 — /analysis route tests (mocked Claude)."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import analysis as analysis_routes
from backend.db import init_db, session_scope
from backend.models.knowledge_graph_cell import KnowledgeGraphCell
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome


pytestmark = [pytest.mark.unit]


@pytest.fixture
def client():
    import backend.db as db_mod
    prev_engine = db_mod._engine
    prev_session = db_mod._SessionLocal
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db(path)
    analysis_routes.clear_thesis_cache()
    app = FastAPI()
    app.include_router(analysis_routes.router)
    try:
        yield TestClient(app)
    finally:
        analysis_routes.clear_thesis_cache()
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


def _seed_cohort(ticker="NVDA", pattern="bull_flag",
                    sample_size=400, posterior=0.71):
    with session_scope() as s:
        s.add(KnowledgeGraphCell(
            ticker=ticker, pattern=pattern, regime="trending_up",
            vol_state="normal", time_bucket="rth", horizon="1d",
            sample_size=sample_size, win_rate=0.69,
            posterior_win_rate=posterior, avg_return_pct=0.024,
            avg_hold_minutes=84.0, confidence_lower=0.65,
            confidence_upper=0.76, sample_split="combined",
        ))
        obs = MarketObservation(
            ticker=ticker, pattern=pattern, timestamp=datetime(2026, 5, 1),
            timeframe="1d", regime="trending_up", spot=470.0,
        )
        s.add(obs)
        s.flush()
        s.add(MarketOutcome(
            observation_id=obs.id, horizon="1d", entry_price=470.0,
            exit_price=480.0, return_pct=0.021, was_winner=True,
        ))


def _fake_df():
    idx = pd.date_range("2026-06-04 09:30", periods=30, freq="5min")
    return pd.DataFrame({
        "open": [100.0 + i * 0.1 for i in range(30)],
        "high": [101.0 + i * 0.1 for i in range(30)],
        "low": [99.0 + i * 0.1 for i in range(30)],
        "close": [100.5 + i * 0.1 for i in range(30)],
        "volume": [1000.0] * 30,
    }, index=idx)


def _fake_bars():
    df = _fake_df()
    return [
        {"t": ts.isoformat(), "open": float(row["open"]),
         "high": float(row["high"]), "low": float(row["low"]),
         "close": float(row["close"]), "volume": float(row["volume"])}
        for ts, row in df.iterrows()
    ]


def test_analysis_returns_expected_shape(client):
    _seed_cohort()
    from backend.bot.analysis import deep_composer as deep_mod
    with patch.object(analysis_routes, "_fetch_bars_dataframe", return_value=_fake_df()), \
            patch.object(analysis_routes, "_fetch_bars", return_value=_fake_bars()), \
            patch.object(analysis_routes, "_run_detectors_in_window",
                              return_value=[{"timestamp": "2026-06-04T15:30:00",
                                                "pattern": "bull_flag",
                                                "family": "candlesticks",
                                                "regime": "trending_up",
                                                "vol_state": "normal",
                                                "time_bucket": "afternoon",
                                                "type": "detector_hit"}]), \
            patch.object(deep_mod, "deep_compose", return_value=None):
        r = client.get("/analysis/NVDA?window=today")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "NVDA"
    assert body["window"] == "today"
    assert isinstance(body["bars"], list) and len(body["bars"]) == 30
    assert isinstance(body["observations"], list)
    assert "bull_flag" in body["knowledge"]
    k = body["knowledge"]["bull_flag"]
    assert k["sample_size"] == 400
    assert k["posterior_win_rate"] == 0.71
    # Phase 14.A — fast composer always produces a thesis even when deep returns None.
    assert "bull_flag" in body["theses"]
    assert isinstance(body["theses"]["bull_flag"]["invalidation"], list)
    # Phase 14.A response payload additions.
    assert "fast_thesis" in body
    assert "uncertainty_signal" in body
    assert "bull_flag" in body["fast_thesis"]


def test_window_parameter_validated(client):
    r = client.get("/analysis/NVDA?window=bogus")
    # FastAPI regex validation returns 422.
    assert r.status_code == 422


def test_suggested_action_gated_by_posterior(client):
    """Cohort below the 60% / N>=30 floor → suggested_action is null."""
    _seed_cohort(posterior=0.45, sample_size=10)
    from backend.bot.analysis import deep_composer as deep_mod
    with patch.object(analysis_routes, "_fetch_bars_dataframe", return_value=_fake_df()), \
            patch.object(analysis_routes, "_fetch_bars", return_value=_fake_bars()), \
            patch.object(analysis_routes, "_run_detectors_in_window",
                              return_value=[{"timestamp": "2026-06-04T15:30:00",
                                                "pattern": "bull_flag",
                                                "family": "candlesticks",
                                                "regime": "trending_up",
                                                "vol_state": "normal",
                                                "time_bucket": "afternoon",
                                                "type": "detector_hit"}]), \
            patch.object(deep_mod, "deep_compose", return_value=None):
        r = client.get("/analysis/NVDA?window=today")
    body = r.json()
    thesis = body["theses"].get("bull_flag", {})
    assert thesis.get("suggested_action") is None


def test_thesis_cache_hits_avoid_repeat_calls(client):
    _seed_cohort()
    call_count = {"n": 0}

    def _compose_mock(*args, **kwargs):
        call_count["n"] += 1
        return None

    from backend.bot.analysis import deep_composer as deep_mod
    with patch.object(analysis_routes, "_fetch_bars_dataframe", return_value=_fake_df()), \
            patch.object(analysis_routes, "_fetch_bars", return_value=_fake_bars()), \
            patch.object(analysis_routes, "_run_detectors_in_window",
                              return_value=[{"timestamp": "2026-06-04T15:30:00",
                                                "pattern": "bull_flag",
                                                "family": "candlesticks",
                                                "regime": "trending_up",
                                                "vol_state": "normal",
                                                "time_bucket": "afternoon",
                                                "type": "detector_hit"}]), \
            patch.object(deep_mod, "deep_compose", side_effect=_compose_mock):
        client.get("/analysis/NVDA?window=today")
        client.get("/analysis/NVDA?window=today")
        client.get("/analysis/NVDA?window=today")
    # 3 page-loads → 1 deep_compose call thanks to the ensemble cache.
    assert call_count["n"] == 1


def test_thesis_cache_differs_by_window(client):
    _seed_cohort()
    call_count = {"n": 0}

    def _compose_mock(*args, **kwargs):
        call_count["n"] += 1
        return None

    from backend.bot.analysis import deep_composer as deep_mod
    with patch.object(analysis_routes, "_fetch_bars_dataframe", return_value=_fake_df()), \
            patch.object(analysis_routes, "_fetch_bars", return_value=_fake_bars()), \
            patch.object(analysis_routes, "_run_detectors_in_window",
                              return_value=[{"timestamp": "2026-06-04T15:30:00",
                                                "pattern": "bull_flag",
                                                "family": "candlesticks",
                                                "regime": "trending_up",
                                                "vol_state": "normal",
                                                "time_bucket": "afternoon",
                                                "type": "detector_hit"}]), \
            patch.object(deep_mod, "deep_compose", side_effect=_compose_mock):
        client.get("/analysis/NVDA?window=today")
        client.get("/analysis/NVDA?window=5d")
    assert call_count["n"] == 2
