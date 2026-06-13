"""MITS Phase 0 — VWAP detector tests."""
from __future__ import annotations

import pandas as pd
import pytest

from backend.bot.detectors.vwap import (
    VWAPReclaimDetector, VWAPRejectionDetector,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _intraday_bars(closes, volumes=None):
    """Build an intraday bar series within a single trading day so
    session VWAP accumulates."""
    n = len(closes)
    idx = pd.date_range("2025-05-01 09:30", periods=n, freq="5min")
    return pd.DataFrame({
        "open": closes, "high": [c * 1.001 for c in closes],
        "low": [c * 0.999 for c in closes], "close": closes,
        "volume": volumes or [100_000] * n,
    }, index=idx)


class TestVWAPReclaim:
    def test_fires_on_cross_up(self):
        # Build a series where price stays below VWAP then crosses.
        # Start high so vwap is high, then crash below, then recover above.
        closes = [110, 108, 106, 100, 95, 90, 92, 95, 98, 102, 108]
        volumes = [200_000] * len(closes)
        bars = _intraday_bars(closes, volumes)
        det = VWAPReclaimDetector()
        obs = det.detect("TEST", bars)
        assert len(obs) >= 1
        assert obs[-1].pattern == "vwap_reclaim"

    def test_silent_when_never_crosses(self):
        closes = [100] * 20
        bars = _intraday_bars(closes)
        det = VWAPReclaimDetector()
        assert det.detect("TEST", bars) == []


class TestVWAPRejection:
    def test_fires_on_cross_down(self):
        # Inverse: low first, then climb above vwap, then drop below.
        closes = [90, 92, 95, 100, 105, 110, 115, 118, 100, 92, 85, 80]
        volumes = [200_000] * len(closes)
        bars = _intraday_bars(closes, volumes)
        det = VWAPRejectionDetector()
        obs = det.detect("TEST", bars)
        assert len(obs) >= 1
