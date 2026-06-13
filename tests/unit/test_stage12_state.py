"""Stage-12.A3 Unified MarketState.

Pinned:
  • Empty inputs → defaults + label="no signal"
  • Composite from real regime/cross_asset/features fills every axis
  • Trend exhaustion: extreme RSI + reversing momentum
  • Vol expanding: high VIX or IV rank
  • Earnings proximity buckets correctly
  • is_risk_off / is_event_imminent predicates work
  • set_latest / get_latest round-trip
  • Endpoints: /state/current (empty + populated), /state/preview
"""
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.state import (
    MarketState,
    build_market_state,
    get_latest,
    reset_latest,
    set_latest,
)


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    reset_latest()
    return TestClient(main_mod.app)


@pytest.fixture(autouse=True)
def _isolate_state():
    reset_latest()
    yield
    reset_latest()


class TestBuildMarketState:
    def test_empty_returns_defaults(self):
        state = build_market_state()
        assert isinstance(state, MarketState)
        assert state.trend == "unknown"
        assert state.label == "no signal"

    def test_composes_from_inputs(self):
        state = build_market_state(
            snapshot={"vix": 18, "adx": 30, "rsi": 60,
                        "macd_hist": 0.5, "prev_macd_hist": 0.3},
            regime={"trend": "bullish", "volatility": "normal",
                       "gamma": "long_gamma", "momentum": "expanding",
                       "risk": "risk_on", "confidence": 0.8},
            cross_asset={"equities": "risk_on", "yields": "rising",
                            "dollar": "weak"},
            features={"iv_rank": 35, "earnings_days": 30},
        )
        assert state.trend == "bullish"
        assert state.trend_phase == "expansion"
        assert state.gamma == "long_gamma"
        assert state.equities == "risk_on"
        assert "bullish" in state.label
        assert "expansion" in state.label

    def test_trend_exhaustion_high_rsi(self):
        state = build_market_state(
            snapshot={"rsi": 80, "macd_hist": 0.1, "prev_macd_hist": 0.5,
                        "adx": 20},
            regime={"trend": "bullish"},
        )
        assert state.trend_phase == "exhaustion"

    def test_trend_exhaustion_low_rsi(self):
        state = build_market_state(
            snapshot={"rsi": 22, "macd_hist": -0.1, "prev_macd_hist": -0.5,
                        "adx": 20},
        )
        assert state.trend_phase == "exhaustion"

    def test_vol_expanding_when_vix_high(self):
        state = build_market_state(snapshot={"vix": 28})
        assert state.vol_phase == "expanding"

    def test_vol_compressing_when_vix_low(self):
        state = build_market_state(
            snapshot={"vix": 12},
            features={"iv_rank": 20},
        )
        assert state.vol_phase == "compressing"

    def test_earnings_proximity(self):
        immediate = build_market_state(features={"earnings_days": 0.5})
        near = build_market_state(features={"earnings_days": 5})
        far = build_market_state(features={"earnings_days": 30})
        assert immediate.earnings_proximity == "immediate"
        assert near.earnings_proximity == "near"
        assert far.earnings_proximity == "far"

    def test_predicates(self):
        risk_off = build_market_state(
            snapshot={"vix": 28},
            regime={"risk": "risk_off"},
        )
        assert risk_off.is_risk_off()
        assert risk_off.is_vol_expanding()

        event = build_market_state(features={"earnings_days": 0})
        assert event.is_event_imminent()

    def test_sources_record_partials(self):
        state = build_market_state(snapshot={"vix": 16})
        assert state.sources["snapshot"] is True
        assert state.sources["regime"] is False


class TestLatestCache:
    def test_round_trip(self):
        state = build_market_state(regime={"trend": "bullish"})
        set_latest(state)
        assert get_latest() is state
        reset_latest()
        assert get_latest() is None


class TestStateEndpoints:
    def test_current_empty(self, client):
        body = client.get("/state/current").json()
        assert body["state"] is None
        assert "reason" in body

    def test_current_after_set(self, client):
        set_latest(build_market_state(regime={"trend": "bullish"}))
        body = client.get("/state/current").json()
        assert body["state"]["trend"] == "bullish"

    def test_preview_endpoint(self, client):
        body = client.post("/state/preview", json={
            "regime": {"trend": "bearish", "volatility": "high"},
            "snapshot": {"vix": 30},
        }).json()
        assert body["state"]["trend"] == "bearish"
        assert body["state"]["vol_phase"] == "expanding"
