"""MITS Phase 0 — detector base + registry tests."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from backend.bot.detectors import (
    DETECTOR_REGISTRY, Observation, all_detectors, detect_all,
)
from backend.bot.detectors.base import (
    Detector, _classify_regime, _classify_vol_state, _time_bucket,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _flat_bars(n: int = 50, base: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": [base] * n,
        "high": [base * 1.001] * n,
        "low": [base * 0.999] * n,
        "close": [base] * n,
        "volume": [1_000_000] * n,
    }, index=idx)


class TestObservationDataclass:
    def test_observation_minimal_fields(self):
        ts = datetime(2025, 5, 1, 14, 30)
        obs = Observation(ticker="SPY", pattern="bull_flag", timestamp=ts)
        assert obs.ticker == "SPY"
        assert obs.pattern == "bull_flag"
        assert obs.timestamp == ts
        # Defaults applied.
        assert obs.timeframe == "1d"
        assert obs.regime == "unknown"
        assert obs.vol_state == "normal"
        assert obs.time_bucket == "rth"
        assert obs.features == {}
        assert obs.source == "historical_replay"

    def test_to_dict_roundtrip(self):
        ts = datetime(2025, 5, 1, 14, 30)
        obs = Observation(
            ticker="NVDA", pattern="bos", timestamp=ts,
            timeframe="1h", regime="trending_up", vol_state="high",
            time_bucket="morning", spot=900.5,
            features={"swing_price": 895.0},
        )
        d = obs.to_dict()
        assert d["ticker"] == "NVDA"
        assert d["pattern"] == "bos"
        assert d["timestamp"] == ts.isoformat()
        assert d["features"] == {"swing_price": 895.0}
        assert d["spot"] == 900.5


class TestDetectorRegistry:
    def test_registry_non_empty(self):
        # 15 TA-Lib + 8 price action + 2 mkt structure + 2 liquidity
        # + 2 vwap + 2 vol profile + 3 options intel = 34
        assert len(DETECTOR_REGISTRY) >= 30
        # Spot-check each family is present.
        for slug in ("bull_flag", "bear_flag", "breakout", "pullback",
                          "failed_breakout", "failed_breakdown",
                          "bos", "choch", "liquidity_sweep", "stop_hunt",
                          "vwap_reclaim", "vwap_rejection",
                          "hvn_acceptance", "lvn_rejection",
                          "iv_expansion", "iv_compression", "gex_acceleration",
                          "talib_engulfing", "talib_hammer", "talib_doji"):
            assert slug in DETECTOR_REGISTRY, f"missing detector: {slug}"

    def test_every_detector_subclass(self):
        for det in all_detectors():
            assert isinstance(det, Detector)
            assert det.pattern, "detector missing pattern slug"

    def test_detect_all_with_empty_bars(self):
        empty = pd.DataFrame()
        assert detect_all("SPY", empty) == []

    def test_detect_all_with_flat_noise_returns_safe(self):
        # Flat bars: candlestick patterns mostly silent, structural
        # detectors find nothing. Function must not raise.
        bars = _flat_bars(60)
        result = detect_all("SPY", bars)
        assert isinstance(result, list)


class TestHelpers:
    def test_time_bucket_classification(self):
        morning = datetime(2025, 5, 1, 10, 15)
        afternoon = datetime(2025, 5, 1, 14, 45)
        post = datetime(2025, 5, 1, 17, 0)
        assert _time_bucket(morning) == "morning"
        assert _time_bucket(afternoon) == "afternoon"
        assert _time_bucket(post) == "post"

    def test_classify_regime_trending_up(self):
        n = 30
        idx = pd.date_range("2025-01-01", periods=n, freq="D")
        closes = [100 + i * 1.5 for i in range(n)]  # strong uptrend
        bars = pd.DataFrame({
            "open": closes, "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes], "close": closes,
            "volume": [1_000_000] * n,
        }, index=idx)
        assert _classify_regime(bars, n - 1) == "trending_up"

    def test_classify_regime_trending_down(self):
        n = 30
        idx = pd.date_range("2025-01-01", periods=n, freq="D")
        closes = [100 - i * 1.5 for i in range(n)]
        bars = pd.DataFrame({
            "open": closes, "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes], "close": closes,
            "volume": [1_000_000] * n,
        }, index=idx)
        assert _classify_regime(bars, n - 1) == "trending_down"

    def test_classify_vol_state_default_normal_on_short(self):
        bars = _flat_bars(10)
        assert _classify_vol_state(bars, 5) == "normal"
