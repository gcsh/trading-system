"""Stage-12.B4 Data Quality Score."""
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.data_quality import QualityScore, score_data_quality


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class TestScoreDataQuality:
    def test_perfect_inputs_excellent(self):
        snapshot = {"price": 100, "rsi": 50, "macd": 0.1, "ma50": 95,
                      "ma200": 90, "vix": 16, "iv_rank": 40,
                      "volume": 1_000_000, "avg_volume": 900_000}
        s = score_data_quality(snapshot=snapshot, source_errors=[])
        assert isinstance(s, QualityScore)
        assert s.band == "excellent"
        assert s.confidence_multiplier == 1.0
        assert not s.should_abstain

    def test_one_feed_error_drops_score(self):
        snapshot = {"price": 100, "rsi": 50, "macd": 0.1, "ma50": 95,
                      "ma200": 90, "vix": 16, "iv_rank": 40,
                      "volume": 1_000_000, "avg_volume": 900_000}
        s = score_data_quality(snapshot=snapshot,
                                  source_errors=["news: timeout"])
        # news feed score drops to 75; others 100 → mean ~ 95 → composite still good-ish
        assert s.feed_scores["news"] < 100
        assert s.feed_scores["price"] == 100
        assert s.band in ("excellent", "good")

    def test_many_errors_trigger_abstain(self):
        s = score_data_quality(
            snapshot={},
            source_errors=["price: down", "price: down", "price: down", "price: down",
                            "options: stale", "options: stale", "options: stale", "options: stale",
                            "news: timeout", "flow: missing"],
        )
        assert s.band == "poor"
        assert s.should_abstain
        assert s.confidence_multiplier < 1.0

    def test_completeness_alone(self):
        # All feeds fine, but no snapshot at all → low completeness → degraded
        s = score_data_quality(snapshot={}, source_errors=[])
        assert s.completeness == 0
        # 0.55 * 100 + 0.45 * 0 = 55 → degraded
        assert 50 <= s.composite < 70

    def test_stale_feed_reported(self):
        s = score_data_quality(
            snapshot={"price": 100, "rsi": 50, "macd": 0,
                        "ma50": 95, "ma200": 90, "vix": 14, "iv_rank": 50,
                        "volume": 1_000_000, "avg_volume": 1_000_000},
            feed_health={"news": {"minutes_since_last_success": 120}},
        )
        # news is past dead threshold → marked stale
        assert "news" in s.stale_feeds


class TestEndpoints:
    def test_score_endpoint(self, client):
        body = client.post("/data-quality/score", json={
            "snapshot": {"price": 100, "rsi": 50, "vix": 16},
            "source_errors": [],
        }).json()
        assert "composite" in body and "band" in body

    def test_current_endpoint(self, client):
        body = client.get("/data-quality/current").json()
        assert "composite" in body
