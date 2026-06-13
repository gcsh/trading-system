"""MITS Phase 0 — options-intel detector tests."""
from __future__ import annotations

import pandas as pd
import pytest

from backend.bot.detectors.options_intel import (
    GEXAccelerationDetector, IVCompressionDetector, IVExpansionDetector,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _bars(n: int = 30):
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": [100.0] * n, "high": [100.5] * n,
        "low": [99.5] * n, "close": [100.0] * n,
        "volume": [1_000_000] * n,
    }, index=idx)


class TestIVExpansion:
    def test_fires_on_iv_jump(self):
        n = 30
        bars = _bars(n)
        # IV: 0.20 for 25 bars, then 0.30 (50% jump > 20% threshold).
        iv = [0.20] * 25 + [0.30, 0.30, 0.30, 0.30, 0.30]
        det = IVExpansionDetector()
        obs = det.detect("TEST", bars, iv_series=iv)
        assert len(obs) >= 1
        last = obs[-1]
        assert last.pattern == "iv_expansion"
        assert last.features["iv_jump_pct"] > 0.20

    def test_silent_when_iv_series_missing(self):
        det = IVExpansionDetector()
        assert det.detect("TEST", _bars(30)) == []
        assert det.detect("TEST", _bars(30), iv_series=None) == []


class TestIVCompression:
    def test_fires_on_iv_drop(self):
        n = 30
        bars = _bars(n)
        iv = [0.40] * 25 + [0.25, 0.25, 0.25, 0.25, 0.25]
        det = IVCompressionDetector()
        obs = det.detect("TEST", bars, iv_series=iv)
        assert len(obs) >= 1


class TestGEXAcceleration:
    def test_fires_on_z_score_spike(self):
        n = 30
        bars = _bars(n)
        # GEX stable then jumps several stdevs.
        gex = [1.0] * 25 + [10.0, 10.0, 10.0, 10.0, 10.0]
        det = GEXAccelerationDetector()
        obs = det.detect("TEST", bars, gex_series=gex)
        assert len(obs) >= 1
        assert obs[-1].features["z_score"] > 2.0

    def test_silent_when_gex_series_missing(self):
        det = GEXAccelerationDetector()
        assert det.detect("TEST", _bars(30)) == []
