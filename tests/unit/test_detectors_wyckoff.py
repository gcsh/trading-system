"""MITS Phase 12.C — Wyckoff detector unit tests."""
from __future__ import annotations

import pandas as pd
import pytest

from backend.bot.detectors.wyckoff import (
    WyckoffAccumulationPhaseDetector,
    WyckoffDistributionPhaseDetector,
    WyckoffSOSDetector,
    WyckoffSpringDetector,
    WyckoffUpthrustDetector,
    build_wyckoff_detectors,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _df(highs, lows, closes, opens=None, volumes=None):
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": opens or closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes or [1_000_000] * n,
    }, index=idx)


def _flat(n=80, price=100.0):
    return _df([price + 0.1] * n, [price - 0.1] * n, [price] * n)


def test_wyckoff_registry():
    dets = build_wyckoff_detectors()
    assert len(dets) == 5
    names = {d.pattern for d in dets}
    assert names == {
        "wyckoff_accumulation_phase",
        "wyckoff_distribution_phase",
        "wyckoff_spring", "wyckoff_sos", "wyckoff_upthrust",
    }
    for d in dets:
        assert d.family == "wyckoff"


class TestAccumulationPhase:
    def test_runs_after_drawdown(self):
        # Drawdown then range then breakout.
        closes = ([100 - i * 0.4 for i in range(40)]
                       + [60 + (i % 5) * 0.5 for i in range(60)]
                       + [60 + i * 0.3 for i in range(20)])
        highs = [c + 1.0 for c in closes]
        lows = [c - 1.0 for c in closes]
        bars = _df(highs, lows, closes)
        out = WyckoffAccumulationPhaseDetector().detect("WACC", bars)
        assert isinstance(out, list)

    def test_silent_on_flat(self):
        out = WyckoffAccumulationPhaseDetector().detect("F", _flat())
        assert out == []


class TestDistributionPhase:
    def test_runs_after_runup(self):
        closes = ([100 + i * 0.4 for i in range(40)]
                       + [120 + (i % 5) * 0.5 for i in range(60)]
                       + [120 - i * 0.3 for i in range(20)])
        highs = [c + 1.0 for c in closes]
        lows = [c - 1.0 for c in closes]
        bars = _df(highs, lows, closes)
        out = WyckoffDistributionPhaseDetector().detect("WDIST", bars)
        assert isinstance(out, list)


class TestSpring:
    def test_runs_without_error(self):
        n = 80
        # 40-bar range around 100, then break below and recover.
        closes = ([100 + (i % 5) * 0.4 for i in range(40)]
                       + [97, 96, 98, 100, 101, 102, 103, 104,
                          105, 106])
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        # Spike low on bar 41 (break) with reduced volume; rising volume after.
        lows[40] = 95.0
        volumes = [1_000_000] * 40 + [600_000] + [1_500_000] * 9
        bars = _df(highs, lows, closes, volumes=volumes)
        out = WyckoffSpringDetector().detect("SPRING", bars)
        assert isinstance(out, list)

    def test_silent_on_flat(self):
        assert WyckoffSpringDetector().detect("F", _flat()) == []


class TestSOS:
    def test_runs_without_error(self):
        n = 70
        # 40-bar range with a low-touch test then a strong breakout above.
        closes = ([100 + (i % 6) * 0.3 for i in range(40)] +
                       [101, 99.5, 102, 105, 108, 110,
                        112, 114, 113, 115])
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        # Volume spike on breakout day (index 45).
        volumes = [1_000_000] * 40 + [1_000_000, 1_000_000, 1_500_000,
                                                2_500_000, 3_000_000, 2_800_000,
                                                1_500_000, 1_400_000, 1_300_000,
                                                1_200_000]
        bars = _df(highs, lows, closes, volumes=volumes)
        out = WyckoffSOSDetector().detect("SOS", bars)
        assert isinstance(out, list)


class TestUpthrust:
    def test_runs_without_error(self):
        n = 80
        closes = ([100 + (i % 5) * 0.3 for i in range(40)] +
                       [102, 103, 101.5, 100, 99,
                        98, 97, 96, 95, 94])
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        # False breakout spike on bar 40.
        highs[40] = 108.0
        volumes = [1_000_000] * 40 + [500_000] + [1_500_000] * 9
        bars = _df(highs, lows, closes, volumes=volumes)
        out = WyckoffUpthrustDetector().detect("UT", bars)
        assert isinstance(out, list)
