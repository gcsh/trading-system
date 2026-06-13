"""MITS Phase 0 — auto-bootstrap-on-watchlist-add test.

Verifies that adding a ticker via POST /watchlist kicks off the
background corpus pipeline and the corpus_status row transitions.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend.bot.corpus import auto_bootstrap
from backend.bot.corpus.historical_replay import bootstrap_ticker
from backend.db import init_db, session_scope
from backend.models.corpus_status import CorpusStatus
from backend.models.market_observation import MarketObservation


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


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


def _synthetic_daily(n: int = 150):
    closes = [100 + i * 0.5 for i in range(n)]
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [1_000_000] * n,
    }, index=idx)


def test_run_full_bootstrap_writes_observations_and_status(fresh_db):
    bars = _synthetic_daily(150)
    with patch("backend.bot.corpus.historical_replay._fetch_daily_bars",
                  return_value=bars), \
            patch("backend.bot.corpus.historical_replay._fetch_intraday_bars",
                  return_value=None), \
            patch("backend.bot.corpus.outcome_linker._fetch_bars_for_outcome",
                  return_value=bars):
        result = auto_bootstrap.run_full_bootstrap("BOOT")
    assert result["status"] in {"ready", "error"}
    with session_scope() as s:
        n_obs = s.query(MarketObservation).filter_by(ticker="BOOT").count()
        status = s.query(CorpusStatus).filter_by(ticker="BOOT").first()
        assert n_obs >= 1
        assert status is not None
        assert status.observation_count == n_obs


def test_watchlist_add_triggers_corpus_pipeline(fresh_db, monkeypatch):
    """End-to-end: POST /watchlist with mocked yfinance → corpus_status row appears."""
    # Patch every network call so the background threads don't hit yfinance.
    bars = _synthetic_daily(120)
    monkeypatch.setattr(
        "backend.bot.corpus.historical_replay._fetch_daily_bars",
        lambda *a, **kw: bars,
    )
    monkeypatch.setattr(
        "backend.bot.corpus.historical_replay._fetch_intraday_bars",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "backend.bot.corpus.outcome_linker._fetch_bars_for_outcome",
        lambda *a, **kw: bars,
    )
    # IV warm-start path: stub the backfill so it doesn't network either.
    monkeypatch.setattr(
        "backend.bot.data.iv_history.backfill",
        lambda *a, **kw: {"inserted": 0, "skipped": 0, "errors": 0},
    )
    # finnhub / yf quote path used by _enrich.
    monkeypatch.setattr(
        "backend.bot.data.finnhub.FinnhubClient.available", False,
    )
    monkeypatch.setattr(
        "backend.api.routes.watchlist._yf_quote",
        lambda ticker: None,
    )

    # Build a minimal app with only the watchlist router.
    from fastapi import FastAPI
    from backend.api.routes import watchlist as wl_routes
    app = FastAPI()
    app.include_router(wl_routes.router)
    client = TestClient(app)

    response = client.post("/watchlist", json={"ticker": "WLBOOT"})
    assert response.status_code == 200

    # The background thread should populate corpus_status quickly given
    # mocked bars. Poll for up to 8 seconds.
    deadline = time.time() + 8.0
    final = None
    while time.time() < deadline:
        with session_scope() as s:
            row = s.query(CorpusStatus).filter_by(ticker="WLBOOT").first()
            if row is not None:
                final = row.to_dict()
                if final["status"] in {"ready", "error", "insufficient"}:
                    break
        time.sleep(0.25)
    assert final is not None
    assert final["status"] in {"ready", "error", "insufficient", "building"}
