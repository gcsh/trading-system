"""MITS Phase 5 (P5.2) — /prediction-outcomes route tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import prediction_outcomes as route_mod
from backend.db import init_db, session_scope
from backend.models.eod_analysis import EodAnalysis
from backend.models.eod_prediction_outcome import (
    EodPredictionOutcome, OUTCOME_NOT_TRADED, OUTCOME_TRADED_MATCHED,
)


@pytest.fixture
def app(tmp_path, monkeypatch):
    db_file = tmp_path / "route_test.db"
    monkeypatch.setattr(
        "backend.config.SETTINGS.db_path", str(db_file),
    )
    import backend.db as db_mod
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db(str(db_file))
    app = FastAPI()
    app.include_router(route_mod.router)
    yield app
    db_mod._engine = None
    db_mod._SessionLocal = None


def _seed(session, ticker, posterior=0.80, n=120, traded=True,
          outcome=OUTCOME_TRADED_MATCHED, pnl=120.0):
    today = datetime.utcnow().date()
    ea = EodAnalysis(
        ticker=ticker, analysis_date=today,
        patterns_fired=json.dumps(["bull_flag"]),
        top_pattern="bull_flag",
        top_posterior=posterior, top_sample_size=n,
        rank_score=4.0,
    )
    session.add(ea)
    session.flush()
    row = EodPredictionOutcome(
        eod_analysis_id=ea.id, ticker=ticker,
        analysis_date=today,
        predicted_direction="long_call",
        posterior=posterior, sample_size=n,
        traded=1 if traded else 0,
        outcome=outcome,
        actual_pnl_dollars=pnl,
        skip_reason=None if traded else "catalyst_gate",
    )
    session.add(row)


def test_get_list_returns_rows(app):
    with session_scope() as s:
        _seed(s, "AAPL")
        _seed(s, "TSLA")
    client = TestClient(app)
    res = client.get("/prediction-outcomes?limit=10")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 2
    tickers = {r["ticker"] for r in body["rows"]}
    assert tickers == {"AAPL", "TSLA"}


def test_get_list_filters_by_date(app):
    with session_scope() as s:
        _seed(s, "AAPL")
    client = TestClient(app)
    today = datetime.utcnow().date().isoformat()
    res = client.get(f"/prediction-outcomes?date={today}")
    assert res.status_code == 200
    assert res.json()["count"] == 1
    yesterday = (datetime.utcnow().date() - timedelta(days=2)).isoformat()
    res = client.get(f"/prediction-outcomes?date={yesterday}")
    assert res.json()["count"] == 0


def test_get_list_bad_date_returns_400(app):
    client = TestClient(app)
    res = client.get("/prediction-outcomes?date=not-a-date")
    assert res.status_code == 400


def test_accuracy_aggregates(app):
    with session_scope() as s:
        # 3 high-conviction (all traded), 1 not_traded with low pnl
        _seed(s, "AAA", posterior=0.85, n=120, traded=True,
              outcome=OUTCOME_TRADED_MATCHED, pnl=100.0)
        _seed(s, "BBB", posterior=0.85, n=120, traded=True,
              outcome=OUTCOME_TRADED_MATCHED, pnl=-50.0)
        _seed(s, "CCC", posterior=0.85, n=120, traded=True,
              outcome=OUTCOME_TRADED_MATCHED, pnl=200.0)
        _seed(s, "DDD", posterior=0.85, n=120, traded=False,
              outcome=OUTCOME_NOT_TRADED, pnl=None)
    client = TestClient(app)
    res = client.get("/prediction-outcomes/accuracy?window=30")
    assert res.status_code == 200
    body = res.json()
    assert body["high_conviction_total"] == 4
    assert body["high_conviction_traded"] == 3
    assert body["high_conviction_act_rate"] == pytest.approx(3 / 4)
    assert body["closed_wins"] == 2
    assert body["closed_losses"] == 1
    assert body["closed_win_rate"] == pytest.approx(2 / 3)
    assert body["realized_pnl_dollars"] == pytest.approx(250.0)


def test_accuracy_all_window(app):
    client = TestClient(app)
    res = client.get("/prediction-outcomes/accuracy?window=all")
    assert res.status_code == 200
    body = res.json()
    assert body["window"] == "all"


def test_reconcile_endpoint(app):
    with session_scope() as s:
        today = datetime.utcnow().date()
        s.add(EodAnalysis(
            ticker="QQQ", analysis_date=today,
            patterns_fired=json.dumps(["bull_flag"]),
            top_pattern="bull_flag",
            top_posterior=0.80, top_sample_size=120,
            rank_score=5.0,
            suggested_action_json=json.dumps({
                "action": "BUY_CALL", "direction": "long_call",
                "strike": 400.0, "dte": 30,
            }),
        ))
    client = TestClient(app)
    res = client.post("/prediction-outcomes/reconcile")
    assert res.status_code == 200
    body = res.json()
    # No trade was seeded so the QQQ row should land as not_traded.
    assert body["not_traded"] == 1
