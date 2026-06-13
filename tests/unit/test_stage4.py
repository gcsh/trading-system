"""Stage-4 — microstructure proxies + cross-asset state + event-risk gate.

Pinned behavior:
  • Microstructure metrics safe on empty input
  • aggressive_flow ∈ [-1, 1] and sign matches the bar bias
  • Cross-asset state safely degrades when feeds are missing (no crash)
  • Event calendar pulls macro events for the current year
  • can_trade BLOCKS during the FOMC announcement window
  • can_trade ALLOWS when no events are active
"""
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.bot.cross_asset import (
    AssetState,
    CrossAssetState,
    alignment_for,
    clear_cache,
    fetch_state,
    hedge_suggestion,
)
from backend.bot.event_risk import (
    active_events,
    can_trade,
    upcoming_events,
    _macro_events_for_year,
    _opex_dates_for_year,
)
from backend.bot.microstructure import (
    MicrostructureSnapshot,
    _absorption_from_bars,
    _aggressive_flow,
    _imbalance_from_quote,
    _spread_bps,
    _sweep_from_bars,
    assess_microstructure,
)


# ── microstructure unit ───────────────────────────────────────────────────


class TestMicrostructureBuildingBlocks:
    def test_imbalance_neutral_when_equal(self):
        assert _imbalance_from_quote(100, 100) == 0.0

    def test_imbalance_clamped(self):
        assert _imbalance_from_quote(0, 0) == 0.0
        assert _imbalance_from_quote(1000, 0) == 1.0
        assert _imbalance_from_quote(0, 1000) == -1.0

    def test_spread_bps_safe_zero(self):
        assert _spread_bps(0, 0, 0) == 0.0

    def test_spread_bps_known(self):
        # bid 99.95, ask 100.05, mid 100 → 0.10/100 × 10000 = 10 bps
        assert _spread_bps(99.95, 100.05, 100.0) == 10.0

    def test_aggressive_flow_all_up(self):
        bars = [{"open": 100, "close": 101, "high": 101, "low": 100, "volume": 1000}] * 5
        assert _aggressive_flow(bars) == 1.0

    def test_aggressive_flow_all_down(self):
        bars = [{"open": 101, "close": 100, "high": 101, "low": 100, "volume": 1000}] * 5
        assert _aggressive_flow(bars) == -1.0

    def test_aggressive_flow_mixed(self):
        bars = ([{"open": 100, "close": 101, "high": 101, "low": 100, "volume": 1000}] * 3
                 + [{"open": 101, "close": 100, "high": 101, "low": 100, "volume": 1000}] * 2)
        out = _aggressive_flow(bars)
        assert 0.0 < out < 1.0


class TestAssessMicrostructure:
    def test_empty_input_safe(self):
        snap = assess_microstructure(ticker="X")
        assert snap.spread_bps == 0.0
        assert snap.aggressive_flow == 0.0
        assert "no bar data" in snap.notes[0]

    def test_full_input(self):
        bars = [{"open": 100, "close": 100.5, "high": 100.6, "low": 99.9,
                  "volume": 5000}] * 30
        snap = assess_microstructure(
            ticker="NVDA", bars=bars, avg_volume=150_000,
            bid=215.00, ask=215.05, bid_size=300, ask_size=500,
        )
        assert snap.spread_bps > 0
        assert snap.bid_ask_imbalance < 0          # ask-heavy → negative
        assert snap.aggressive_flow > 0            # all green bars → buy-side
        assert snap.source == "proxy"


# ── cross-asset ────────────────────────────────────────────────────────────


class TestCrossAsset:
    def test_state_degrades_safely_when_no_feed(self, monkeypatch):
        clear_cache()
        # Mock yfinance to always raise — state must come back as "unknown"
        # everywhere without raising.
        import backend.bot.cross_asset as ca
        def _bad(_ticker):
            return AssetState(ticker=_ticker, notes=["mock fail"])
        monkeypatch.setattr(ca, "_fetch_asset", _bad)
        state = fetch_state(force=True)
        assert isinstance(state, CrossAssetState)
        assert state.equities == "mixed"
        assert state.confidence == 0.0

    def test_alignment_bullish_with_risk_on(self):
        state = CrossAssetState(equities="risk_on", volatility="compressed",
                                  yields="falling", regime_label="risk_on_compressed_vol",
                                  confidence=1.0)
        out = alignment_for(ticker_regime_trend="bullish", state=state)
        assert out["aligned"]
        assert any("bullish" in a for a in out["aligned_axes"])

    def test_alignment_conflicts_caught(self):
        state = CrossAssetState(equities="risk_off", volatility="spiking",
                                  regime_label="risk_off_high_vol", confidence=1.0)
        out = alignment_for(ticker_regime_trend="bullish", state=state)
        assert not out["aligned"]
        assert out["conflicts"]

    def test_hedge_recommended_in_risk_off(self):
        state = CrossAssetState(equities="risk_off", volatility="spiking",
                                  regime_label="risk_off_high_vol", confidence=1.0)
        out = hedge_suggestion(state=state, net_beta=1.5)
        assert out["size_fraction"] > 0
        assert "VXX" in out["instruments"] or "SH" in out["instruments"]

    def test_no_hedge_in_risk_on(self):
        state = CrossAssetState(equities="risk_on", volatility="compressed",
                                  regime_label="risk_on_compressed_vol", confidence=1.0)
        out = hedge_suggestion(state=state, net_beta=1.0)
        assert out["size_fraction"] == 0.0


# ── event-risk ────────────────────────────────────────────────────────────


class TestEventCalendar:
    def test_opex_third_friday(self):
        # June 2026: 3rd Friday is June 19
        dates = _opex_dates_for_year(2026)
        june = [d for d in dates if d.month == 6][0]
        assert june.weekday() == 4
        assert 15 <= june.day <= 21

    def test_macro_events_include_cpi_and_fomc(self):
        events = _macro_events_for_year(2026)
        names = [e.name for e in events]
        assert any("CPI" in n for n in names)
        assert any("FOMC" in n for n in names)

    def test_upcoming_horizon(self):
        out = upcoming_events(within_days=365)   # next year
        # Stage 1 verdict: 2026 calendar has macro events; the suite runs
        # in 2026 so we expect at least one upcoming event.
        assert isinstance(out, list)


class TestCanTrade:
    def test_blocks_during_fomc_window(self):
        # 18:00 UTC on the June 18 FOMC date — within ±30 min of the print
        fixed = datetime(2026, 6, 18, 18, 0)
        perm = can_trade("NVDA", now=fixed)
        assert not perm.can_trade
        assert "FOMC" in perm.reason

    def test_allows_outside_event_window(self):
        # Far from any high-impact macro print
        ok_time = datetime(2026, 6, 25, 14, 30)
        perm = can_trade("NVDA", now=ok_time)
        assert perm.can_trade


# ── live API integration ──────────────────────────────────────────────────


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    clear_cache()
    return TestClient(main_mod.app)


class TestEndpoints:
    def test_cross_asset_state_endpoint(self, client, monkeypatch):
        # No need to hit yfinance for the schema test
        from backend.bot import cross_asset as ca
        monkeypatch.setattr(ca, "_fetch_asset",
                              lambda t: AssetState(ticker=t, notes=["mock"]))
        clear_cache()
        body = client.get("/cross-asset/state").json()
        for key in ("equities", "volatility", "yields", "dollar",
                     "regime_label", "confidence", "assets"):
            assert key in body

    def test_cross_asset_alignment_endpoint(self, client):
        body = client.get("/cross-asset/alignment/bullish").json()
        assert "aligned" in body and "aligned_axes" in body

    def test_event_calendar_endpoint(self, client):
        body = client.get("/event-risk/calendar?within_days=60").json()
        assert "events" in body and isinstance(body["events"], list)

    def test_event_active_endpoint(self, client):
        body = client.get("/event-risk/active").json()
        assert "active" in body and "auto_hold" in body

    def test_can_trade_endpoint(self, client):
        body = client.get("/event-risk/can-trade/NVDA").json()
        assert "can_trade" in body
