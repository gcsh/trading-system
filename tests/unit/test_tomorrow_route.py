"""MITS Phase 3 — /tomorrow route tests."""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import date
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import tomorrow as tomorrow_routes
from backend.db import init_db, session_scope
from backend.models.eod_analysis import EodAnalysis


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
    app = FastAPI()
    app.include_router(tomorrow_routes.router)
    try:
        yield TestClient(app)
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


def _seed(ticker, score, analysis_date=date(2026, 6, 5)):
    with session_scope() as s:
        s.add(EodAnalysis(
            ticker=ticker, analysis_date=analysis_date,
            patterns_fired=json.dumps(["bull_flag"]),
            top_pattern="bull_flag",
            top_posterior=0.7, top_sample_size=300,
            headline=f"{ticker} headline",
            thesis_paragraph="paragraph",
            invalidation_json=json.dumps([]),
            rank_score=score,
        ))


def test_get_tomorrow_returns_rank_ordered(client):
    _seed("NVDA", score=4.0)
    _seed("SPY", score=2.0)
    _seed("AAPL", score=5.5)
    r = client.get("/tomorrow?date=2026-06-05")
    assert r.status_code == 200
    body = r.json()
    tickers = [row["ticker"] for row in body["rows"]]
    assert tickers == ["AAPL", "NVDA", "SPY"]
    assert body["count"] == 3


def test_get_tomorrow_respects_date_filter(client):
    _seed("NVDA", score=4.0, analysis_date=date(2026, 6, 5))
    _seed("SPY", score=2.0, analysis_date=date(2026, 6, 4))
    r = client.get("/tomorrow?date=2026-06-05")
    body = r.json()
    assert {row["ticker"] for row in body["rows"]} == {"NVDA"}


def test_get_tomorrow_400_on_bad_date(client):
    r = client.get("/tomorrow?date=not-a-date")
    assert r.status_code == 400


def test_get_tomorrow_empty_returns_empty(client):
    r = client.get("/tomorrow?date=1999-01-01")
    body = r.json()
    assert body["rows"] == []
    assert body["count"] == 0


def test_rebuild_triggers_run_eod_pass(client):
    captured = {}

    def _fake_pass(target):
        captured["called_with"] = target
        return {"tickers_analyzed": 0}

    with patch("backend.bot.eod_analysis.run_eod_pass",
                  side_effect=_fake_pass):
        r = client.post("/tomorrow/rebuild?date=2026-06-05")
        assert r.status_code == 200
        # Daemon thread is async — give it a brief tick.
        time.sleep(0.3)
    assert captured.get("called_with") == date(2026, 6, 5)


def test_rebuild_400_on_bad_date(client):
    r = client.post("/tomorrow/rebuild?date=foo")
    assert r.status_code == 400
