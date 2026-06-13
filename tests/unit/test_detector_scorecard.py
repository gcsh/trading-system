"""MITS Phase 6 (P6.2) — Detector scorecard tests."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import detector_scorecard as ds_routes
from backend.bot.scorecard.detector_scorecard import (
    build_detector_scorecard, build_leaderboard, cumulative_pnl_series,
)
from backend.db import init_db, session_scope
from backend.models.trade import Trade


pytestmark = [pytest.mark.unit]


@pytest.fixture
def fresh_db():
    import backend.db as db_mod
    prev_engine = db_mod._engine
    prev_session = db_mod._SessionLocal
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db(path)
    try:
        yield path
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.fixture
def client(fresh_db):
    app = FastAPI()
    app.include_router(ds_routes.router)
    return TestClient(app)


def _seed(*, ticker="NVDA", pnl=100.0, days_ago=2,
              top_pattern="bull_flag", strategy=None,
              instrument="stock", price=400.0, quantity=10):
    ts = datetime.utcnow() - timedelta(days=days_ago)
    detail = {}
    if top_pattern:
        detail["eod_bias"] = {"top_pattern": top_pattern}
    with session_scope() as s:
        t = Trade(
            timestamp=ts, ticker=ticker, action="BUY",
            quantity=quantity, price=price,
            strategy=strategy or top_pattern or "x",
            signal_source="eod_bias", confidence=0.7,
            reason="seed", paper=1, pnl=pnl, status="closed",
            instrument=instrument,
            detail_json=json.dumps(detail) if detail else None,
        )
        s.add(t)
        s.flush()


def test_scorecard_aggregates_correctly(fresh_db):
    _seed(top_pattern="bull_flag", pnl=200.0)
    _seed(top_pattern="bull_flag", pnl=-50.0)
    _seed(top_pattern="bull_flag", pnl=100.0)
    card = build_detector_scorecard("bull_flag", window="30")
    assert card["closed_trades"] == 3
    assert card["win_count"] == 2
    assert card["loss_count"] == 1
    assert card["realized_pnl_dollars"] == 250.0
    assert abs(card["win_rate"] - 2 / 3) < 1e-4


def test_window_filters_old_trades(fresh_db):
    _seed(top_pattern="bull_flag", pnl=100.0, days_ago=2)
    _seed(top_pattern="bull_flag", pnl=200.0, days_ago=45)
    card7 = build_detector_scorecard("bull_flag", window="7")
    card30 = build_detector_scorecard("bull_flag", window="30")
    cardall = build_detector_scorecard("bull_flag", window="all")
    assert card7["closed_trades"] == 1
    assert card30["closed_trades"] == 1
    assert cardall["closed_trades"] == 2


def test_attribution_decay_weights_recent_higher(fresh_db):
    # Two equal-PNL trades at very different times should produce a
    # higher attribution score from the recent one.
    _seed(top_pattern="bull_flag", pnl=100.0, days_ago=1)
    card_recent = build_detector_scorecard("bull_flag", window="all")
    # Wipe and re-seed an old one only.
    with session_scope() as s:
        s.query(Trade).delete()
    _seed(top_pattern="bull_flag", pnl=100.0, days_ago=60)
    card_old = build_detector_scorecard("bull_flag", window="all")
    assert card_recent["attribution_score"] > card_old["attribution_score"]


def test_leaderboard_sorted_by_attribution(fresh_db):
    _seed(top_pattern="winner_pat", pnl=500.0)
    _seed(top_pattern="loser_pat", pnl=-200.0)
    rows = build_leaderboard(window="30")
    names = [r["detector_name"] for r in rows
                if r["status"] == "active"]
    # "winner_pat" comes before "loser_pat" because its attribution
    # is positive while loser's is negative.
    assert names.index("winner_pat") < names.index("loser_pat")


def test_route_returns_payload(client, fresh_db):
    _seed(top_pattern="bull_flag", pnl=120.0)
    r = client.get("/detectors/bull_flag/scorecard?window=30")
    assert r.status_code == 200
    body = r.json()
    assert body["detector_name"] == "bull_flag"
    assert body["closed_trades"] >= 1
    # Series only included when asked.
    assert "pnl_series" not in body
    r2 = client.get("/detectors/bull_flag/scorecard?window=30&include_series=true")
    assert r2.status_code == 200
    assert "pnl_series" in r2.json()


def test_leaderboard_route(client, fresh_db):
    _seed(top_pattern="bull_flag", pnl=80.0)
    r = client.get("/detectors/scorecard?window=30")
    assert r.status_code == 200
    body = r.json()
    assert "detectors" in body
    assert any(d["detector_name"] == "bull_flag" for d in body["detectors"])


def test_invalid_window_400(client):
    r = client.get("/detectors/scorecard?window=bogus")
    assert r.status_code == 400
