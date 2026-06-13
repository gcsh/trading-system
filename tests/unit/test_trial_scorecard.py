"""MITS Phase 6 (P6.5) — $5k paper trial scorecard tests."""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import trial_scorecard as ts_routes
from backend.api.routes.trial_scorecard import (
    _classify_projection, _max_drawdown, _sharpe, _trading_days_elapsed,
)
from backend.config import TUNABLES
from backend.db import init_db, session_scope
from backend.models.paper import PaperAccount
from backend.models.snapshot import PortfolioSnapshot


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
    app.include_router(ts_routes.router)
    return TestClient(app)


def _seed_snapshot(value: float, ts: datetime):
    with session_scope() as s:
        s.add(PortfolioSnapshot(
            timestamp=ts,
            portfolio_value=value,
            cash=value, realized_pnl=0.0,
            open_positions=0, broker="local_paper",
            data_quality="good", accounting_version=2,
        ))


def test_classify_projection_on_track():
    # $5k starting, day 10/30, target_growth=5% over the trial. By day
    # 10 we should be at $5,000 + (10/30)*$250 = $5,083. $5,100 clears.
    proj = _classify_projection(
        current=5_100.0, starting=5_000.0,
        days_elapsed=10, days_total=30,
        target_growth_pct=0.05,
        breach_floor_pct=0.85,
    )
    assert proj == "on_track"


def test_classify_projection_off_track():
    proj = _classify_projection(
        current=4_900.0, starting=5_000.0,
        days_elapsed=15, days_total=30,
        target_growth_pct=0.05,
        breach_floor_pct=0.85,
    )
    assert proj == "off_track"


def test_classify_projection_breached():
    proj = _classify_projection(
        current=4_000.0, starting=5_000.0,
        days_elapsed=10, days_total=30,
        target_growth_pct=0.05,
        breach_floor_pct=0.85,  # floor = $4250
    )
    assert proj == "breached"


def test_max_drawdown_basic():
    vals = [5000.0, 5200.0, 4800.0, 5100.0, 4900.0]
    dd = _max_drawdown(vals)
    # Peak at 5200, trough 4800 → ~7.69%
    assert abs(dd["pct"] - (400.0 / 5200.0)) < 1e-3
    assert dd["dollars"] == 400.0


def test_max_drawdown_empty():
    assert _max_drawdown([]) == {"pct": 0.0, "dollars": 0.0}


def test_sharpe_needs_at_least_two_points():
    assert _sharpe([0.01]) is None
    # Two equal returns → 0 std → None.
    assert _sharpe([0.01, 0.01]) is None


def test_sharpe_positive_for_consistent_gains():
    # Mostly-positive series should give a positive sharpe.
    rets = [0.005, 0.004, 0.006, 0.003, 0.005, 0.004]
    s = _sharpe(rets)
    assert s is not None
    assert s > 0


def test_trading_days_elapsed_counts_weekdays_only():
    # 2026-05-25 (Mon) through 2026-06-05 (Fri) = 10 weekdays.
    start = date(2026, 5, 25)
    today = date(2026, 6, 5)
    assert _trading_days_elapsed(start, today) == 10


def test_route_returns_payload(client, fresh_db):
    # Seed a paper account so the fallback equity path triggers.
    with session_scope() as s:
        s.add(PaperAccount(
            starting_cash=5000.0, cash=5000.0,
            realized_pnl=0.0, last_portfolio_value=5050.0,
        ))
    r = client.get("/trial-scorecard")
    assert r.status_code == 200
    body = r.json()
    # Required keys exist.
    for k in ("starting_equity", "current_equity",
              "total_return_pct", "total_return_dollars",
              "trial_start_date", "trial_end_date",
              "weekly_pnl_predicted_vs_realized",
              "high_conviction_setups_total",
              "max_drawdown_pct", "projection", "narrative"):
        assert k in body, f"missing {k}"
    assert body["starting_equity"] == TUNABLES.trial_starting_equity
    # Narrative falls back when no ANTHROPIC key is set.
    assert isinstance(body["narrative"], str)
    assert len(body["narrative"]) > 0


def test_route_uses_snapshot_when_available(client, fresh_db):
    start = datetime.combine(
        date.fromisoformat(TUNABLES.trial_start_date),
        datetime.min.time(),
    )
    _seed_snapshot(5000.0, start + timedelta(hours=1))
    _seed_snapshot(5150.0, start + timedelta(days=1))
    r = client.get("/trial-scorecard")
    body = r.json()
    assert body["current_equity"] == 5150.0
    assert abs(body["total_return_pct"] - 0.03) < 1e-3
