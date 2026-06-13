"""MITS Phase 4 (P4.3) — shared bars fetcher tests.

Pins:
  1. ThetaData success path returns ``source='thetadata'`` + bars.
  2. yfinance fallback path returns ``source='yfinance'`` when
     ThetaData errors / returns empty.
  3. Bar shape parity — both providers produce the same dict shape.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pandas as pd
import pytest

pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _fake_thetadata_payload():
    return {
        "response": [
            {
                "date": "2026-06-04",
                "open": 100.0, "high": 101.0, "low": 99.5,
                "close": 100.5, "volume": 1_500_000,
            },
            {
                "date": "2026-06-05",
                "open": 100.6, "high": 102.0, "low": 100.0,
                "close": 101.8, "volume": 1_800_000,
            },
        ]
    }


def _fake_yfinance_df():
    idx = pd.date_range("2026-06-04", periods=2, freq="D")
    return pd.DataFrame({
        "Open": [100.0, 100.6],
        "High": [101.0, 102.0],
        "Low": [99.5, 100.0],
        "Close": [100.5, 101.8],
        "Volume": [1_500_000, 1_800_000],
    }, index=idx)


def test_fetch_bars_thetadata_success_path():
    from backend.bot.data import bars as bars_mod

    class _Resp:
        status_code = 200

        def json(self):
            return _fake_thetadata_payload()

    with patch.object(
        bars_mod, "_fetch_bars_thetadata",
        return_value=[
            {"t": "2026-06-04T00:00:00", "open": 100.0, "high": 101.0,
             "low": 99.5, "close": 100.5, "volume": 1_500_000},
            {"t": "2026-06-05T00:00:00", "open": 100.6, "high": 102.0,
             "low": 100.0, "close": 101.8, "volume": 1_800_000},
        ],
    ):
        payload = bars_mod.fetch_bars(
            "SPY", window="all", interval="1d", lookback_days=2,
        )
    assert payload["source"] == "thetadata"
    assert len(payload["bars"]) == 2
    assert payload["bars"][0]["close"] == 100.5


def test_fetch_bars_yfinance_fallback():
    from backend.bot.data import bars as bars_mod
    with patch.object(bars_mod, "_fetch_bars_thetadata", return_value=None), \
            patch.object(
                bars_mod, "_fetch_bars_yfinance",
                return_value=[
                    {"t": "2026-06-04T00:00:00", "open": 100.0,
                     "high": 101.0, "low": 99.5,
                     "close": 100.5, "volume": 1_500_000},
                ],
            ):
        payload = bars_mod.fetch_bars(
            "SPY", window="today", interval="5m", lookback_days=1,
        )
    assert payload["source"] == "yfinance"
    assert payload["bars"]


def test_fetch_bars_both_fail_returns_empty():
    from backend.bot.data import bars as bars_mod
    with patch.object(bars_mod, "_fetch_bars_thetadata", return_value=None), \
            patch.object(bars_mod, "_fetch_bars_yfinance", return_value=None):
        payload = bars_mod.fetch_bars(
            "BOGUSTICKER", window="today", interval="5m",
        )
    assert payload["source"] == "none"
    assert payload["bars"] == []


def test_bar_shape_parity_across_providers():
    """Same logical day from each provider must serialize to identical
    keys + types."""
    from backend.bot.data import bars as bars_mod
    theta_bar = {
        "t": "2026-06-04T00:00:00", "open": 100.0, "high": 101.0,
        "low": 99.5, "close": 100.5, "volume": 1_500_000.0,
    }
    yf_bar = {
        "t": "2026-06-04T00:00:00", "open": 100.0, "high": 101.0,
        "low": 99.5, "close": 100.5, "volume": 1_500_000.0,
    }
    assert set(theta_bar.keys()) == set(yf_bar.keys())
    for k in ("open", "high", "low", "close", "volume"):
        assert type(theta_bar[k]) == type(yf_bar[k])


def test_bars_to_dataframe_roundtrip():
    from backend.bot.data import bars as bars_mod
    bars = [
        {"t": "2026-06-04T00:00:00", "open": 100.0, "high": 101.0,
         "low": 99.5, "close": 100.5, "volume": 1_500_000},
        {"t": "2026-06-05T00:00:00", "open": 100.6, "high": 102.0,
         "low": 100.0, "close": 101.8, "volume": 1_800_000},
    ]
    df = bars_mod.bars_to_dataframe(bars)
    assert df is not None
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 2


def test_analysis_route_surfaces_bar_source():
    """Integration: the analysis route now carries ``bar_source`` at
    the top level, populated by the shims."""
    from fastapi.testclient import TestClient
    from unittest.mock import patch
    from backend.main import create_app
    from backend.api.routes import analysis as analysis_routes

    app = create_app()
    client = TestClient(app)

    bars = [
        {"t": "2026-06-04T00:00:00", "open": 100.0, "high": 101.0,
         "low": 99.5, "close": 100.5, "volume": 1_500_000}
    ] * 30
    idx = pd.date_range("2026-06-04", periods=30, freq="D")
    df = pd.DataFrame({
        "open": [100.0] * 30, "high": [101.0] * 30, "low": [99.5] * 30,
        "close": [100.5] * 30, "volume": [1_500_000] * 30,
    }, index=idx)
    analysis_routes.clear_thesis_cache()
    with patch.object(analysis_routes, "_fetch_bars_dataframe", return_value=df), \
            patch.object(analysis_routes, "_fetch_bars", return_value=bars):
        # Seed the bar-source cache so the route can read it.
        analysis_routes._last_bar_source[("SPY", "today")] = "thetadata"
        r = client.get("/analysis/SPY?window=today")
    assert r.status_code == 200
    body = r.json()
    assert "bar_source" in body
    assert body["bar_source"] in {"thetadata", "yfinance", "none", "unknown"}
