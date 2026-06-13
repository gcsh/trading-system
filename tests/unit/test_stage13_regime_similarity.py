"""Stage-13.C5 Regime Similarity Engine."""
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.bot.regime_similarity import (
    aggregate_outcomes,
    find_similar,
    snapshot_current,
)
from backend.bot.state import build_market_state, set_latest, reset_latest


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    reset_latest()
    return TestClient(main_mod.app)


@pytest.fixture(autouse=True)
def _isolate():
    reset_latest()
    yield
    reset_latest()


def _seed_snapshot(*, trend="bullish", vix=15, iv_rank=40, vol_phase="neutral",
                     gamma="long_gamma", equities="risk_on",
                     yields="rising", breadth_score=0.5, sentiment_score=0.3,
                     sector_strength=0.4, fwd_1d=None, fwd_5d=None,
                     trades=0, wins=0, pnl=0.0, ts_offset_min=0):
    """Insert a RegimeEpisodeSnapshot directly."""
    from backend.db import session_scope
    from backend.models.regime_episode import RegimeEpisodeSnapshot
    with session_scope() as s:
        row = RegimeEpisodeSnapshot(
            trend=trend, trend_phase="neutral", volatility="normal",
            vol_phase=vol_phase, gamma=gamma, risk="neutral",
            equities=equities, yields=yields, dollar="neutral",
            label=f"{trend} test",
            vix=vix, iv_rank=iv_rank, breadth_score=breadth_score,
            sentiment_score=sentiment_score, sector_strength=sector_strength,
            fwd_1d_return=fwd_1d, fwd_5d_return=fwd_5d,
            fwd_trades_count=trades, fwd_trades_wins=wins, fwd_trades_pnl=pnl,
        )
        row.timestamp = datetime.utcnow() + timedelta(minutes=ts_offset_min)
        s.add(row); s.flush()
        return row.id


class TestSnapshotCurrent:
    def test_writes_from_market_state(self, temp_db):
        state = build_market_state(
            regime={"trend": "bullish", "volatility": "normal",
                       "gamma": "long_gamma"},
            features={"vix": 16, "iv_rank": 40},
        )
        sid = snapshot_current(state, breadth_score=0.4, sentiment_score=0.2)
        assert sid is not None
        from backend.db import session_scope
        from backend.models.regime_episode import RegimeEpisodeSnapshot
        with session_scope() as s:
            row = s.get(RegimeEpisodeSnapshot, sid)
            assert row.trend == "bullish"
            assert row.vix == 16
            assert row.breadth_score == 0.4


class TestFindSimilar:
    def test_empty_table(self, temp_db):
        out = find_similar({"trend": "bullish"})
        assert out == []

    def test_finds_match_on_categorical_axes(self, temp_db):
        _seed_snapshot(trend="bullish", vol_phase="neutral",
                          gamma="long_gamma", equities="risk_on",
                          yields="rising", vix=15, breadth_score=0.5)
        _seed_snapshot(trend="bearish", vol_phase="expanding",
                          gamma="short_gamma", equities="risk_off",
                          yields="falling", vix=28, breadth_score=-0.5,
                          ts_offset_min=10)
        target = {"trend": "bullish", "vol_phase": "neutral",
                    "gamma": "long_gamma", "equities": "risk_on",
                    "yields": "rising", "vix": 16, "iv_rank": 40,
                    "breadth_score": 0.4, "sentiment_score": 0.3,
                    "sector_strength": 0.4}
        matches = find_similar(target, k=5, min_similarity=0.4)
        assert len(matches) >= 1
        # Most similar match is the bullish snapshot
        assert matches[0].snapshot["trend"] == "bullish"

    def test_min_similarity_filters(self, temp_db):
        _seed_snapshot(trend="bearish", gamma="short_gamma",
                          equities="risk_off", yields="falling", vix=30)
        # Target opposite of everything → very low similarity
        target = {"trend": "bullish", "vol_phase": "neutral",
                    "gamma": "long_gamma", "equities": "risk_on",
                    "yields": "rising", "vix": 14}
        matches = find_similar(target, min_similarity=0.7)
        assert matches == []


class TestAggregateOutcomes:
    def test_empty(self):
        out = aggregate_outcomes([])
        assert out["matches"] == 0
        assert out["win_rate"] is None

    def test_rolls_up(self, temp_db):
        _seed_snapshot(trend="bullish", fwd_1d=0.01, fwd_5d=0.03,
                          trades=10, wins=6, pnl=200.0)
        _seed_snapshot(trend="bullish", fwd_1d=0.02, fwd_5d=0.04,
                          trades=20, wins=12, pnl=300.0, ts_offset_min=10)
        target = {"trend": "bullish", "vol_phase": "neutral",
                    "gamma": "long_gamma", "equities": "risk_on",
                    "yields": "rising"}
        matches = find_similar(target, min_similarity=0.4)
        agg = aggregate_outcomes(matches)
        assert agg["matches"] == 2
        assert agg["trades_count"] == 30
        # 18/30 = 0.6
        assert agg["win_rate"] == 0.6
        assert agg["total_pnl"] == 500.0
        assert agg["mean_fwd_1d"] is not None


class TestEndpoints:
    def test_similar_endpoint(self, client, temp_db):
        _seed_snapshot(trend="bullish", vix=15)
        body = client.post("/regimes/similar", json={
            "target": {"trend": "bullish", "vol_phase": "neutral",
                          "gamma": "long_gamma", "equities": "risk_on",
                          "yields": "rising", "vix": 16},
            "k": 5, "min_similarity": 0.4,
        }).json()
        assert "matches" in body and "summary" in body

    def test_similar_current_no_state(self, client):
        body = client.get("/regimes/similar/current").json()
        assert body["matches"] == []
        assert "reason" in body

    def test_similar_current_with_state(self, client, temp_db):
        _seed_snapshot(trend="bullish", vix=15)
        state = build_market_state(
            regime={"trend": "bullish", "gamma": "long_gamma"},
            cross_asset={"equities": "risk_on", "yields": "rising"},
            features={"vix": 16},
        )
        set_latest(state)
        body = client.get("/regimes/similar/current").json()
        assert len(body["matches"]) >= 1

    def test_snapshot_endpoint(self, client):
        state = build_market_state(regime={"trend": "bullish"})
        set_latest(state)
        body = client.post("/regimes/snapshot", json={
            "breadth_score": 0.3, "sentiment_score": 0.2,
        }).json()
        assert body["snapshot_id"] is not None
