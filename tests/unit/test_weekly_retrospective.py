"""MITS Phase 6 (P6.4) — Weekly retrospective tests."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import retrospective as retro_routes
from backend.bot.retrospective import (
    build_weekly_retrospective, monday_of_week,
)
from backend.db import init_db, session_scope
from backend.models.trade import Trade
from backend.models.weekly_retrospective import WeeklyRetrospective


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
    app.include_router(retro_routes.router)
    return TestClient(app)


def _seed_trade_in_week(monday: date, *, ticker="NVDA",
                                  pnl=100.0, day_offset=2,
                                  top_pattern="bull_flag",
                                  rank=1):
    ts = datetime.combine(monday, datetime.min.time()) + \
        timedelta(days=day_offset, hours=14)
    detail = {"eod_bias": {"top_pattern": top_pattern,
                                  "rank": rank}}
    with session_scope() as s:
        t = Trade(
            timestamp=ts, ticker=ticker, action="BUY", quantity=10,
            price=400.0, strategy=top_pattern,
            signal_source="eod_bias", confidence=0.7,
            reason="seed", paper=1, pnl=pnl, status="closed",
            instrument="stock",
            detail_json=json.dumps(detail),
        )
        s.add(t)


def test_monday_of_week_helper():
    sun = date(2026, 6, 7)
    mon = date(2026, 6, 1)
    assert monday_of_week(sun) == mon
    assert monday_of_week(mon) == mon


def test_build_aggregates_pnl_and_winners(fresh_db):
    monday = date(2026, 5, 25)
    _seed_trade_in_week(monday, ticker="NVDA", pnl=300.0)
    _seed_trade_in_week(monday, ticker="AAPL", pnl=-100.0,
                                  top_pattern="bear_flag")
    _seed_trade_in_week(monday, ticker="NVDA", pnl=150.0)
    row = build_weekly_retrospective(monday, allow_claude=False)
    d = row.to_dict()
    assert d["closed_trades"] == 3
    assert d["realized_pnl_dollars"] == 350.0  # 300 + (-100) + 150
    # NVDA was the biggest winner.
    assert d["top_winning_tickers"][0]["key"] == "NVDA"
    # AAPL the biggest loser.
    assert d["top_losing_tickers"][0]["key"] == "AAPL"
    assert d["summary_paragraph"]
    assert d["summary_source"] == "fallback"


def test_upsert_overwrites_existing(fresh_db):
    monday = date(2026, 5, 18)
    _seed_trade_in_week(monday, pnl=100.0)
    row1 = build_weekly_retrospective(monday, allow_claude=False)
    assert row1.closed_trades == 1
    _seed_trade_in_week(monday, pnl=200.0)
    row2 = build_weekly_retrospective(monday, allow_claude=False)
    assert row2.closed_trades == 2
    # Single row in the table — UPSERT, not insert.
    with session_scope() as s:
        n = s.query(WeeklyRetrospective).count()
        assert n == 1


def test_top_winning_patterns_ordering(fresh_db):
    monday = date(2026, 5, 11)
    _seed_trade_in_week(monday, top_pattern="bull_flag", pnl=400.0)
    _seed_trade_in_week(monday, top_pattern="hammer", pnl=120.0)
    _seed_trade_in_week(monday, top_pattern="hammer", pnl=80.0)
    row = build_weekly_retrospective(monday, allow_claude=False)
    d = row.to_dict()
    patterns = [p["key"] for p in d["top_winning_patterns"]]
    assert patterns[0] == "bull_flag"  # 400 > 200


def test_conviction_buckets_split_correctly(fresh_db):
    monday = date(2026, 5, 4)
    _seed_trade_in_week(monday, pnl=200.0, rank=1)
    _seed_trade_in_week(monday, pnl=50.0, rank=2)
    _seed_trade_in_week(monday, pnl=-30.0, rank=5)
    row = build_weekly_retrospective(monday, allow_claude=False)
    eff = row.to_dict()["conviction_multiplier_pnl_effect"]
    assert "rank_1" in eff
    assert eff["rank_1"]["pnl_dollars"] == 200.0
    assert eff["rank_2_3"]["pnl_dollars"] == 50.0
    assert eff["rank_4_plus"]["pnl_dollars"] == -30.0


def test_route_returns_present_false_when_missing(client, fresh_db):
    r = client.get("/retrospective?week=2025-01-06")
    assert r.status_code == 200
    body = r.json()
    assert body["present"] is False


def test_route_rebuild_then_get(client, fresh_db):
    monday = date(2026, 4, 27)
    _seed_trade_in_week(monday, pnl=125.0)
    r = client.get(f"/retrospective?week={monday.isoformat()}&rebuild=true")
    assert r.status_code == 200
    body = r.json()
    assert body["present"] is True
    assert body["closed_trades"] == 1


def test_route_list_returns_recent(client, fresh_db):
    for offset in range(3):
        m = date(2026, 4, 20) - timedelta(weeks=offset)
        _seed_trade_in_week(m, pnl=50.0)
        build_weekly_retrospective(m, allow_claude=False)
    r = client.get("/retrospective/list?limit=5")
    assert r.status_code == 200
    arr = r.json()
    assert len(arr) >= 3
