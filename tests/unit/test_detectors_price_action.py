"""MITS Phase 0 — price action detector tests.

Synthetic bar sequences engineered so each detector MUST or MUST NOT
fire. The detectors are intentionally rule-of-thumb (operator decision
#2) — these tests pin the geometry the rule recognises, not academic
correctness.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.bot.detectors.price_action import (
    BearFlagDetector, BreakoutDetector, BullFlagDetector,
    ConsolidationDetector, FailedBreakdownDetector,
    FailedBreakoutDetector, PennantDetector, PullbackDetector,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _df(opens, highs, lows, closes, volumes=None):
    n = len(closes)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volumes or [1_000_000] * n,
    }, index=idx)


def _flat(n: int = 50, base: float = 100.0):
    return _df([base] * n, [base * 1.001] * n, [base * 0.999] * n,
                  [base] * n)


# ── bull flag ─────────────────────────────────────────────────────────


def _bull_flag_bars():
    """Thrust + tight consolidation that holds above the thrust mid."""
    closes = []
    # Build a steady baseline.
    for i in range(10):
        closes.append(100 + i * 0.1)
    # Thrust: 10 bars from 101 -> 115 (+13%)
    thrust = [101, 103, 106, 109, 112, 114, 115, 114.5, 115, 115]
    closes.extend(thrust)
    # 5-bar tight consolidation around 115.
    cons = [115.1, 114.9, 115.2, 114.8, 115.0]
    closes.extend(cons)
    n = len(closes)
    highs = [c * 1.003 for c in closes]
    lows = [c * 0.997 for c in closes]
    opens = closes  # close-on-close geometry
    return _df(opens, highs, lows, closes)


class TestBullFlag:
    def test_fires_on_thrust_plus_consolidation(self):
        bars = _bull_flag_bars()
        det = BullFlagDetector()
        obs = det.detect("TEST", bars)
        assert len(obs) >= 1
        last = obs[-1]
        assert last.pattern == "bull_flag"
        assert "thrust_pct" in last.features
        assert last.features["thrust_pct"] > 0.05

    def test_silent_on_flat_noise(self):
        det = BullFlagDetector()
        assert det.detect("FLAT", _flat(60)) == []


# ── bear flag ─────────────────────────────────────────────────────────


def _bear_flag_bars():
    closes = []
    for i in range(10):
        closes.append(100 - i * 0.1)
    thrust = [99, 97, 94, 91, 88, 86, 85, 85.5, 85, 85]
    closes.extend(thrust)
    cons = [84.9, 85.1, 84.8, 85.2, 85.0]
    closes.extend(cons)
    highs = [c * 1.003 for c in closes]
    lows = [c * 0.997 for c in closes]
    return _df(closes, highs, lows, closes)


class TestBearFlag:
    def test_fires_on_thrust_down_plus_consolidation(self):
        bars = _bear_flag_bars()
        det = BearFlagDetector()
        obs = det.detect("TEST", bars)
        assert len(obs) >= 1
        last = obs[-1]
        assert last.features["thrust_pct"] < -0.05


# ── breakout / pullback ───────────────────────────────────────────────


def _breakout_bars():
    """20 flat bars at 100, then a clean break above with volume."""
    closes = [100.0] * 22
    closes[-1] = 110.0  # clear breakout
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]
    volumes = [1_000_000] * 21 + [3_000_000]
    return _df(closes, highs, lows, closes, volumes=volumes)


class TestBreakout:
    def test_fires_on_volume_breakout(self):
        bars = _breakout_bars()
        det = BreakoutDetector()
        obs = det.detect("TEST", bars)
        assert len(obs) >= 1

    def test_silent_without_volume_expansion(self):
        closes = [100.0] * 22
        closes[-1] = 110.0
        bars = _df(closes, [c * 1.002 for c in closes],
                       [c * 0.998 for c in closes], closes,
                       volumes=[1_000_000] * 22)
        det = BreakoutDetector()
        assert det.detect("TEST", bars) == []


def _pullback_bars():
    """Rising 20-bar trend, then a 1-bar dip."""
    closes = [100 + i * 0.6 for i in range(22)]
    # Force a 1.5% dip on the last bar.
    closes[-1] = closes[-3] * 0.985
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]
    return _df(closes, highs, lows, closes)


class TestPullback:
    def test_fires_in_uptrend(self):
        det = PullbackDetector()
        obs = det.detect("TEST", _pullback_bars())
        assert len(obs) >= 1


# ── failed breakout / breakdown ───────────────────────────────────────


def _failed_breakout_bars():
    """20 bars flat at 100, bar 21 breaks to 102, bar 22 falls back to 99."""
    closes = [100.0] * 20 + [102.5, 99.0]
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]
    return _df(closes, highs, lows, closes)


class TestFailedBreakout:
    def test_fires_when_breakout_reverses(self):
        det = FailedBreakoutDetector()
        obs = det.detect("TEST", _failed_breakout_bars())
        assert len(obs) >= 1


def _failed_breakdown_bars():
    closes = [100.0] * 20 + [97.5, 101.0]
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]
    return _df(closes, highs, lows, closes)


class TestFailedBreakdown:
    def test_fires_when_breakdown_reverses(self):
        det = FailedBreakdownDetector()
        obs = det.detect("TEST", _failed_breakdown_bars())
        assert len(obs) >= 1


# ── consolidation / pennant ───────────────────────────────────────────


def _consolidation_bars():
    """30 bars of normal range, then 10 bars where bar range is tiny."""
    closes = []
    highs = []
    lows = []
    base = 100.0
    for i in range(40):
        closes.append(base)
        if i < 30:
            highs.append(base + 1.0)
            lows.append(base - 1.0)
        else:
            highs.append(base + 0.05)
            lows.append(base - 0.05)
    return _df(closes, highs, lows, closes)


class TestConsolidation:
    def test_fires_when_range_compresses(self):
        det = ConsolidationDetector()
        obs = det.detect("TEST", _consolidation_bars())
        assert len(obs) >= 1


def _pennant_bars():
    """Thrust then shrinking range."""
    closes = []
    highs = []
    lows = []
    for i in range(10):
        closes.append(100 + i * 0.5)
        highs.append(closes[-1] + 0.5)
        lows.append(closes[-1] - 0.5)
    # Thrust last 6 bars.
    for i in range(6):
        closes.append(closes[-1] + 1.5)
        highs.append(closes[-1] + 0.5)
        lows.append(closes[-1] - 0.5)
    # Shrinking range over next 5 bars.
    for width in (1.5, 1.2, 0.9, 0.6, 0.3):
        c = closes[-1]
        closes.append(c)
        highs.append(c + width)
        lows.append(c - width)
    return _df(closes, highs, lows, closes)


class TestPennant:
    def test_fires_after_thrust_with_compression(self):
        det = PennantDetector()
        obs = det.detect("TEST", _pennant_bars())
        assert len(obs) >= 1
