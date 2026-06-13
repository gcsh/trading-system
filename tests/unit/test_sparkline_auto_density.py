"""MITS Phase 2 (P2.4) — knowledge sparkline auto-density.

Locks the contract:
  * `history_days > daily_cap` triggers weekly bucketing.
  * `history_days <= daily_cap` returns daily rows.
  * Weekly bucket math: sample-size-weighted posterior + recomputed CI.
  * `resolution` field is "daily" or "weekly".
"""
from datetime import date, datetime, timedelta

import pytest


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


@pytest.fixture(autouse=True)
def _fresh_db_and_app(tmp_path):
    """Spin a fresh DB + minimal FastAPI app with the knowledge router."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import backend.db as _dbmod
    from backend.api.routes import knowledge as knowledge_routes
    from backend.db import init_db

    prev_engine = _dbmod._engine
    prev_session = _dbmod._SessionLocal
    db_path = tmp_path / "sparkline_test.sqlite"
    _dbmod._engine = None
    _dbmod._SessionLocal = None
    init_db(str(db_path))
    app = FastAPI()
    app.include_router(knowledge_routes.router)
    client = TestClient(app)
    yield client
    _dbmod._engine = prev_engine
    _dbmod._SessionLocal = prev_session


def _seed_cell_with_history(ticker: str, pattern: str, *,
                                  days: int, start_date: date = None,
                                  posterior_at_day=None):
    from backend.db import session_scope
    from backend.models.knowledge_graph_cell import KnowledgeGraphCell
    from backend.models.knowledge_graph_history import KnowledgeGraphHistory

    if start_date is None:
        start_date = date.today() - timedelta(days=days)
    if posterior_at_day is None:
        posterior_at_day = lambda d: 0.60 + (d * 0.001)

    with session_scope() as s:
        cell = KnowledgeGraphCell(
            ticker=ticker, pattern=pattern,
            regime="trending_up", vol_state="normal",
            time_bucket="rth", horizon="1d",
            sample_split="combined",
            sample_size=100, win_rate=0.65,
            posterior_win_rate=0.62, avg_return_pct=0.012,
            confidence_lower=0.55, confidence_upper=0.69,
        )
        s.add(cell)
        for i in range(days):
            d = start_date + timedelta(days=i)
            row = KnowledgeGraphHistory(
                ticker=ticker, pattern=pattern,
                regime="trending_up", vol_state="normal",
                time_bucket="rth", horizon="1d",
                sample_split="combined",
                snapshot_date=d,
                sample_size=100,
                win_rate=0.65,
                posterior_win_rate=posterior_at_day(i),
                avg_return_pct=0.012,
                confidence_lower=0.55,
                confidence_upper=0.69,
            )
            s.add(row)


class TestAutoDensity:
    def test_below_cap_returns_daily(self, _fresh_db_and_app):
        client = _fresh_db_and_app
        _seed_cell_with_history("AAA", "bull_flag", days=30)
        r = client.get("/knowledge/AAA/bull_flag?history_days=30")
        assert r.status_code == 200
        body = r.json()
        assert body.get("resolution") == "daily"
        hist = body.get("history") or []
        assert len(hist) >= 25  # most of 30 days

    def test_above_cap_triggers_weekly(self, _fresh_db_and_app):
        client = _fresh_db_and_app
        _seed_cell_with_history("BBB", "breakout", days=200)
        r = client.get("/knowledge/BBB/breakout?history_days=200")
        assert r.status_code == 200
        body = r.json()
        assert body.get("resolution") == "weekly"
        hist = body.get("history") or []
        # 200 days ≈ 28-29 weeks.
        assert 25 <= len(hist) <= 32

    def test_weekly_buckets_are_mondays(self, _fresh_db_and_app):
        client = _fresh_db_and_app
        _seed_cell_with_history("CCC", "consolidation", days=200)
        r = client.get("/knowledge/CCC/consolidation?history_days=200")
        body = r.json()
        hist = body.get("history") or []
        for row in hist:
            d = date.fromisoformat(row["snapshot_date"])
            assert d.weekday() == 0, f"non-Monday bucket date: {d}"

    def test_weekly_posterior_is_weighted_average(self, _fresh_db_and_app):
        client = _fresh_db_and_app
        # Seed exactly 7 daily rows spanning one ISO week (Mon..Sun)
        # with different sample sizes so we can verify weighted average.
        from backend.db import session_scope
        from backend.models.knowledge_graph_cell import KnowledgeGraphCell
        from backend.models.knowledge_graph_history import KnowledgeGraphHistory

        # Use a date far enough back that the API includes it.
        start = date.today() - timedelta(days=200)
        # Walk forward to the first Monday.
        while start.weekday() != 0:
            start += timedelta(days=1)

        with session_scope() as s:
            cell = KnowledgeGraphCell(
                ticker="DDD", pattern="bear_flag",
                regime="trending_up", vol_state="normal",
                time_bucket="rth", horizon="1d",
                sample_split="combined",
                sample_size=100, win_rate=0.65,
                posterior_win_rate=0.62, avg_return_pct=0.0,
                confidence_lower=0.55, confidence_upper=0.69,
            )
            s.add(cell)
            # 3 rows with N=10, post=0.40
            # 4 rows with N=20, post=0.60
            for i in range(3):
                s.add(KnowledgeGraphHistory(
                    ticker="DDD", pattern="bear_flag",
                    regime="trending_up", vol_state="normal",
                    time_bucket="rth", horizon="1d",
                    sample_split="combined",
                    snapshot_date=start + timedelta(days=i),
                    sample_size=10, win_rate=0.40,
                    posterior_win_rate=0.40,
                    avg_return_pct=0.01,
                    confidence_lower=0.30, confidence_upper=0.50,
                ))
            for i in range(3, 7):
                s.add(KnowledgeGraphHistory(
                    ticker="DDD", pattern="bear_flag",
                    regime="trending_up", vol_state="normal",
                    time_bucket="rth", horizon="1d",
                    sample_split="combined",
                    snapshot_date=start + timedelta(days=i),
                    sample_size=20, win_rate=0.60,
                    posterior_win_rate=0.60,
                    avg_return_pct=0.02,
                    confidence_lower=0.50, confidence_upper=0.70,
                ))

        r = client.get("/knowledge/DDD/bear_flag?history_days=400")
        body = r.json()
        assert body.get("resolution") == "weekly"
        hist = body.get("history") or []
        # Find the bucket for our start Monday.
        match = [h for h in hist if h["snapshot_date"] == start.isoformat()]
        assert len(match) == 1
        bucket = match[0]
        # Expected weighted posterior:
        #   (3 * 10 * 0.40 + 4 * 20 * 0.60) / (3 * 10 + 4 * 20)
        #   = (12 + 48) / 110 = 60 / 110 ≈ 0.5454
        expected = (3 * 10 * 0.40 + 4 * 20 * 0.60) / (3 * 10 + 4 * 20)
        assert bucket["posterior_win_rate"] == pytest.approx(expected, abs=1e-3)
        assert bucket["sample_size"] == 110

    def test_no_history_days_param_returns_no_resolution(self, _fresh_db_and_app):
        # `history_days=0` keeps the legacy behaviour (no history field).
        client = _fresh_db_and_app
        _seed_cell_with_history("EEE", "pullback", days=30)
        r = client.get("/knowledge/EEE/pullback")
        body = r.json()
        assert "history" not in body
        assert "resolution" not in body
