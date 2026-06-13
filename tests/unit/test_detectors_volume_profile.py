"""MITS Phase 0 — volume profile (HVN / LVN) detector tests."""
from __future__ import annotations

import pandas as pd
import pytest

from backend.bot.detectors.volume_profile import (
    HVNAcceptanceDetector, LVNRejectionDetector,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _df(closes, highs=None, lows=None, volumes=None):
    n = len(closes)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": closes,
        "high": highs if highs is not None else [c * 1.005 for c in closes],
        "low": lows if lows is not None else [c * 0.995 for c in closes],
        "close": closes,
        "volume": volumes if volumes is not None else [1_000_000] * n,
    }, index=idx)


def _hvn_bars():
    """80 bars: 60 of them clustered at 100 with high volume; final
    bar also at 100 → close inside HVN."""
    closes = []
    volumes = []
    # 60 bars at 100 with huge volume.
    for i in range(60):
        closes.append(100.0 + (i % 3) * 0.01)
        volumes.append(5_000_000)
    # 15 spread-out bars at various prices with low volume.
    for i in range(15):
        closes.append(95 + i * 0.5)
        volumes.append(200_000)
    # Final bars settle back to 100.
    for _ in range(10):
        closes.append(100.0)
        volumes.append(1_000_000)
    return _df(closes, volumes=volumes)


def _lvn_bars():
    """Most volume happens at the extremes; middle of the range is a
    low-volume node. Last bar touches the middle then moves away."""
    closes = []
    volumes = []
    # 35 bars at 90 with huge volume.
    for i in range(35):
        closes.append(90.0)
        volumes.append(5_000_000)
    # 35 bars at 110 with huge volume.
    for i in range(35):
        closes.append(110.0)
        volumes.append(5_000_000)
    # Final bar wicks through the LVN (price 100) but closes away from it.
    closes.append(90.0)
    volumes.append(500_000)
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    highs[-1] = 101.0
    lows[-1] = 89.0
    return _df(closes, highs=highs, lows=lows, volumes=volumes)


class TestHVNAcceptance:
    def test_fires_when_close_inside_high_volume_bin(self):
        det = HVNAcceptanceDetector()
        obs = det.detect("TEST", _hvn_bars())
        assert len(obs) >= 1
        last = obs[-1]
        assert "bin_idx" in last.features


class TestLVNRejection:
    def test_fires_when_range_touches_lvn_but_close_moves_away(self):
        det = LVNRejectionDetector()
        obs = det.detect("TEST", _lvn_bars())
        assert len(obs) >= 1
