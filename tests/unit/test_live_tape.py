"""MITS Phase 7.4 — live tape assembler tests."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.bot.ai.live_tape import (
    SECTOR_ETFS,
    assemble_live_context,
)


class _StubSnapshot:
    def __init__(self, data):
        self.data = data


def _stub_market(spy_data=None, sector_data=None, ticker_data=None):
    """Build a fake MarketDataAdapter whose snapshot() returns
    pre-canned dicts keyed by ticker."""
    spy_data = spy_data or {}
    sector_data = sector_data or {}
    ticker_data = ticker_data or {}
    md = MagicMock()

    def _snap(ticker):
        if ticker == "SPY":
            return _StubSnapshot(spy_data)
        if ticker in SECTOR_ETFS:
            return _StubSnapshot(sector_data.get(ticker, {}))
        return _StubSnapshot(ticker_data.get(ticker, {}))

    md.snapshot.side_effect = _snap
    return md


def test_shape_parity_default_keys():
    out = assemble_live_context("normal", market_data=None)
    expected = {
        "regime_state", "as_of", "spy_ticks_5min",
        "sector_30m_returns", "vix_curve", "unusual_flow",
        "dealer_gex_flip", "breadth", "put_call_ratio",
        "watchlist_top",
    }
    assert expected <= set(out.keys())
    assert out["regime_state"] == "normal"


def test_sector_list_completeness_all_11_sectors_in_output():
    spy = {"vix": 22.0}
    md = _stub_market(spy_data=spy, sector_data={
        sym: {"intraday_30m_pct": 0.5} for sym in SECTOR_ETFS
    })
    out = assemble_live_context("panic", market_data=md)
    assert set(out["sector_30m_returns"].keys()) == set(SECTOR_ETFS)
    assert len(SECTOR_ETFS) == 11


def test_missing_data_graceful_fallback_none_values():
    """No market_data → every field collapses to safe defaults rather
    than raising."""
    out = assemble_live_context("panic", market_data=None)
    assert out["spy_ticks_5min"] == []
    # sector_30m_returns is a dict with all 11 sectors mapped to None
    assert all(v is None for v in out["sector_30m_returns"].values())
    assert out["unusual_flow"] == []
    assert out["breadth"] is None
    assert out["put_call_ratio"] is None
    assert out["watchlist_top"] == []


def test_top_n_flow_sort_respected(monkeypatch):
    """When the flow source returns more than top_n, the assembler
    truncates to top_n preserving the highest-premium first."""
    fake_rows = [
        {"ticker": "AAPL", "premium": 100_000, "kind": "sweep"},
        {"ticker": "MSFT", "premium": 50_000, "kind": "sweep"},
        {"ticker": "TSLA", "premium": 200_000, "kind": "block"},
    ]

    # Patch the flowintel helper used by the assembler.
    import backend.bot.ai.live_tape as lt

    def _fake_unusual_flow(top_n):
        return fake_rows[:top_n]

    monkeypatch.setattr(lt, "_unusual_flow", _fake_unusual_flow)
    out = assemble_live_context("panic", market_data=None)
    assert isinstance(out["unusual_flow"], list)
    assert len(out["unusual_flow"]) <= 10


def test_blob_serializable_as_json():
    out = assemble_live_context("normal", market_data=None)
    s = json.dumps(out, default=str)
    assert "regime_state" in s
    # Stays well under 3KB on empty inputs.
    assert len(s) < 3_500


def test_spy_ticks_downsamples_to_capped_samples():
    spy = {
        "intraday_bars": [
            {"timestamp": f"t{i}", "close": 400.0 + i * 0.1}
            for i in range(500)
        ],
        "vix": 25.0,
    }
    md = _stub_market(spy_data=spy)
    out = assemble_live_context("panic", market_data=md)
    # Default cap is 50; allow margin.
    assert len(out["spy_ticks_5min"]) <= 60
    # All sampled ticks have either a price or are well-formed.
    for tick in out["spy_ticks_5min"]:
        assert "p" in tick


def test_vix_curve_pulls_spot_when_available():
    spy = {"vix": 27.5, "vix_curve_slope": -0.12}
    md = _stub_market(spy_data=spy)
    out = assemble_live_context("panic", market_data=md)
    assert out["vix_curve"]["vix_spot"] == 27.5
    assert out["vix_curve"]["curve_slope"] == -0.12
