"""MITS Phase 0 — /knowledge route tests."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import knowledge as knowledge_routes
from backend.bot.corpus.priors_loader import load_default_priors
from backend.db import init_db, session_scope
from backend.models.corpus_status import CorpusStatus
from backend.models.knowledge_graph_cell import KnowledgeGraphCell
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


@pytest.fixture
def app_client():
    """Spin a fresh DB + FastAPI app for each test."""
    import backend.db as db_mod
    prev_engine = db_mod._engine
    prev_session = db_mod._SessionLocal
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db(path)
    app = FastAPI()
    app.include_router(knowledge_routes.router)
    try:
        yield TestClient(app)
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


def _seed_cell(ticker="AAPL", pattern="bull_flag", regime="trending_up",
                  horizon="1d", sample_size=42, win_rate=0.65) -> None:
    with session_scope() as s:
        s.add(KnowledgeGraphCell(
            ticker=ticker, pattern=pattern, regime=regime,
            vol_state="normal", time_bucket="rth", horizon=horizon,
            sample_size=sample_size, win_rate=win_rate,
            posterior_win_rate=win_rate, avg_return_pct=0.012,
            avg_hold_minutes=1440.0, confidence_lower=0.55,
            confidence_upper=0.74,
        ))


def _seed_observation_with_outcome(ticker="AAPL", pattern="bull_flag") -> None:
    with session_scope() as s:
        obs = MarketObservation(
            ticker=ticker, pattern=pattern, timestamp=datetime(2025, 5, 1),
            timeframe="1d", regime="trending_up", spot=180.0,
        )
        s.add(obs)
        s.flush()
        s.add(MarketOutcome(
            observation_id=obs.id, horizon="1d", entry_price=180.0,
            exit_price=185.0, return_pct=0.0278, was_winner=True,
        ))


def test_cells_endpoint_returns_seeded_cells(app_client):
    _seed_cell()
    _seed_cell(ticker="MSFT", pattern="breakout")
    response = app_client.get("/knowledge/cells")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) >= 2
    tickers = {r["ticker"] for r in rows}
    assert {"AAPL", "MSFT"} <= tickers


def test_cells_filter_by_ticker(app_client):
    _seed_cell(ticker="AAPL")
    _seed_cell(ticker="MSFT")
    response = app_client.get("/knowledge/cells?ticker=AAPL")
    assert response.status_code == 200
    rows = response.json()
    assert all(r["ticker"] == "AAPL" for r in rows)


def test_cells_filter_min_samples(app_client):
    _seed_cell(ticker="AAPL", sample_size=5)
    _seed_cell(ticker="MSFT", sample_size=100)
    response = app_client.get("/knowledge/cells?min_samples=10")
    assert response.status_code == 200
    rows = response.json()
    assert all(r["sample_size"] >= 10 for r in rows)


def test_cell_detail_endpoint(app_client):
    _seed_cell()
    _seed_observation_with_outcome()
    response = app_client.get("/knowledge/AAPL/bull_flag")
    assert response.status_code == 200
    body = response.json()
    assert body["primary_cell"]["ticker"] == "AAPL"
    assert body["primary_cell"]["pattern"] == "bull_flag"
    assert isinstance(body["recent_observations"], list)
    assert len(body["recent_observations"]) >= 1
    obs = body["recent_observations"][0]
    assert isinstance(obs["outcomes"], list)
    assert obs["outcomes"][0]["horizon"] == "1d"


def test_cell_detail_404_when_missing(app_client):
    response = app_client.get("/knowledge/NOPE/bull_flag")
    assert response.status_code == 404


def test_observations_recent_endpoint(app_client):
    _seed_observation_with_outcome()
    response = app_client.get("/knowledge/observations/recent?limit=10")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) >= 1
    assert rows[0]["ticker"] == "AAPL"


def test_corpus_status_endpoint(app_client):
    with session_scope() as s:
        s.add(CorpusStatus(ticker="AAPL", status="ready",
                                  observation_count=200, cell_count=15))
    response = app_client.get("/knowledge/corpus/status")
    assert response.status_code == 200
    rows = response.json()
    assert any(r["ticker"] == "AAPL" and r["status"] == "ready" for r in rows)


def test_rebuild_endpoint_returns_202_and_marks_building(app_client):
    response = app_client.post("/knowledge/corpus/rebuild/SPY")
    assert response.status_code == 202
    body = response.json()
    assert body["ticker"] == "SPY"
    assert body["status"] == "building"
    with session_scope() as s:
        row = s.query(CorpusStatus).filter_by(ticker="SPY").first()
        assert row is not None
        assert row.status == "building"


def test_priors_endpoint(app_client):
    load_default_priors()
    response = app_client.get("/knowledge/priors")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) > 10
    for r in rows:
        assert "pattern" in r
        assert 0.0 <= r["prior_win_rate"] <= 1.0
