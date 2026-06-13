"""MITS Phase 14.D — cross-window disagreement integration test.

Pre-populates the analysis-route per-window thesis cache for both
``today`` and ``5d`` with opposite ``suggested_action.action`` values
on the same pattern, then calls ``/analysis/{ticker}?window=today``
and verifies the response surfaces ``window_disagreement=True`` plus a
non-empty ``reconciler_note``.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import analysis as analysis_routes
from backend.db import init_db, session_scope
from backend.models.brain_prediction import BrainPrediction
from backend.models.knowledge_graph_cell import KnowledgeGraphCell


pytestmark = [pytest.mark.integration]


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


def _pre_populate_cache(ticker, window, action):
    """Seed the per-window thesis cache with a chosen-thesis payload
    whose suggested_action.action equals ``action``."""
    analysis_routes._cache_put((ticker, window), {
        "chosen": {
            "bull_flag": {
                "source": "deep",
                "headline": f"Headline for {window}",
                "thesis_paragraph": f"Paragraph for {window}",
                "suggested_action": {
                    "action": action,
                    "direction": ("long_call" if action == "BUY_CALL"
                                  else "long_put"),
                    "strike": 120.0,
                    "dte": 30,
                },
                "invalidation": ["Position closes below VWAP"],
                "confidence_self_assessment": 0.7,
            }
        },
        "summary": "",
        "fast": {},
        "uncertainty_signal": {},
    })


def test_cross_window_disagreement_emitted(client):
    """today=BUY_CALL vs 5d=BUY_PUT on the same pattern → response
    must include window_disagreement=True + a non-empty reconciler_note.
    """
    _seed_cohort()
    # Pre-populate the parallel (5d) cache with the OPPOSITE action.
    _pre_populate_cache("NVDA", "5d", "BUY_PUT")
    # Also seed the today cache with BUY_CALL so the route returns the
    # cached payload directly (no need to mock the deep composer).
    _pre_populate_cache("NVDA", "today", "BUY_CALL")

    obs = [{"timestamp": "2026-06-04T15:30:00", "pattern": "bull_flag",
             "family": "candlesticks", "regime": "trending_up",
             "vol_state": "normal", "time_bucket": "afternoon",
             "type": "detector_hit"}]
    with patch.object(analysis_routes, "_fetch_bars_dataframe",
                          return_value=_fake_df()), \
            patch.object(analysis_routes, "_fetch_bars",
                              return_value=_fake_bars()), \
            patch.object(analysis_routes, "_run_detectors_in_window",
                              return_value=obs):
        r = client.get("/analysis/NVDA?window=today")
    assert r.status_code == 200
    body = r.json()
    assert "window_disagreement" in body
    assert "reconciler_note" in body
    assert body["window_disagreement"] is True
    assert "BUY_CALL" in body["reconciler_note"]
    assert "BUY_PUT" in body["reconciler_note"]


def test_no_disagreement_when_actions_agree(client):
    _seed_cohort()
    _pre_populate_cache("NVDA", "5d", "BUY_CALL")
    _pre_populate_cache("NVDA", "today", "BUY_CALL")
    obs = [{"timestamp": "2026-06-04T15:30:00", "pattern": "bull_flag",
             "family": "candlesticks", "regime": "trending_up",
             "vol_state": "normal", "time_bucket": "afternoon",
             "type": "detector_hit"}]
    with patch.object(analysis_routes, "_fetch_bars_dataframe",
                          return_value=_fake_df()), \
            patch.object(analysis_routes, "_fetch_bars",
                              return_value=_fake_bars()), \
            patch.object(analysis_routes, "_run_detectors_in_window",
                              return_value=obs):
        r = client.get("/analysis/NVDA?window=today")
    body = r.json()
    assert body["window_disagreement"] is False
    assert body["reconciler_note"] == ""


def test_analysis_writes_brain_prediction_row(client):
    """A single /analysis call with a directional cohort must write at
    least one BrainPrediction row."""
    _seed_cohort()
    _pre_populate_cache("NVDA", "today", "BUY_CALL")
    obs = [{"timestamp": "2026-06-04T15:30:00", "pattern": "bull_flag",
             "family": "candlesticks", "regime": "trending_up",
             "vol_state": "normal", "time_bucket": "afternoon",
             "type": "detector_hit"}]
    with patch.object(analysis_routes, "_fetch_bars_dataframe",
                          return_value=_fake_df()), \
            patch.object(analysis_routes, "_fetch_bars",
                              return_value=_fake_bars()), \
            patch.object(analysis_routes, "_run_detectors_in_window",
                              return_value=obs):
        r = client.get("/analysis/NVDA?window=today")
    assert r.status_code == 200
    with session_scope() as s:
        rows = s.query(BrainPrediction).filter(
            BrainPrediction.ticker == "NVDA").all()
        assert len(rows) >= 1
        row = rows[0]
        assert row.surface == "analysis"
        assert row.suggested_action == "BUY_CALL"
        assert row.pattern == "bull_flag"
