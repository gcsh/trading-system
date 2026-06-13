"""MITS Phase 0 — liquidity detector tests."""
from __future__ import annotations

import pandas as pd
import pytest

from backend.bot.detectors.liquidity import (
    LiquiditySweepDetector, StopHuntDetector,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _df(opens, highs, lows, closes):
    n = len(closes)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1_000_000] * n,
    }, index=idx)


def _sweep_above_bars():
    """10 prior bars with highs <= 100. Final bar wicks to 102 but
    closes at 99 (back inside the range)."""
    closes = []
    highs = []
    lows = []
    opens = []
    for i in range(15):
        c = 99.5
        closes.append(c)
        opens.append(c)
        highs.append(100.0)
        lows.append(99.0)
    # Sweep bar.
    opens.append(99.5)
    closes.append(99.0)
    highs.append(102.0)         # wick well above prior_high (100)
    lows.append(98.5)
    return _df(opens, highs, lows, closes)


def _sweep_below_bars():
    closes = []
    highs = []
    lows = []
    opens = []
    for i in range(15):
        c = 100.5
        closes.append(c)
        opens.append(c)
        highs.append(101.0)
        lows.append(100.0)
    opens.append(100.5)
    closes.append(101.0)
    highs.append(101.5)
    lows.append(98.0)           # wick well below prior_low (100)
    return _df(opens, highs, lows, closes)


class TestLiquiditySweep:
    def test_fires_above_prior_high(self):
        det = LiquiditySweepDetector()
        obs = det.detect("TEST", _sweep_above_bars())
        assert len(obs) >= 1
        assert obs[-1].features["direction"] == "above"

    def test_fires_below_prior_low(self):
        det = LiquiditySweepDetector()
        obs = det.detect("TEST", _sweep_below_bars())
        assert len(obs) >= 1
        assert obs[-1].features["direction"] == "below"

    def test_silent_when_close_outside_range(self):
        # Standard breakout (no return inside) — should NOT fire as a sweep.
        closes = [99.0] * 15 + [101.5]
        bars = _df(closes, [c + 0.5 for c in closes],
                       [c - 0.5 for c in closes], closes)
        # Patch the last bar high to be above the prior high but close also above.
        bars.iloc[-1, bars.columns.get_loc("high")] = 102.0
        bars.iloc[-1, bars.columns.get_loc("close")] = 101.5
        det = LiquiditySweepDetector()
        obs = det.detect("TEST", bars)
        # Either empty or no "above" sweep.
        for o in obs:
            assert o.features["direction"] != "above"


class TestStopHunt:
    def test_fires_on_bear_stop_hunt(self):
        # Bar with high > prior_high, close in bottom third.
        closes = [99.0] * 15 + [97.5]
        opens = list(closes)
        # Wide bar last: high 102, low 97, close 97.5 (bottom third).
        highs = [99.5] * 15 + [102.0]
        lows = [98.5] * 15 + [97.0]
        bars = _df(opens, highs, lows, closes)
        det = StopHuntDetector()
        obs = det.detect("TEST", bars)
        assert any(o.features.get("direction") == "bear" for o in obs)

    def test_fires_on_bull_stop_hunt(self):
        closes = [100.0] * 15 + [102.5]
        opens = list(closes)
        highs = [100.5] * 15 + [103.0]
        lows = [99.5] * 15 + [97.0]   # wicks below prior_low
        bars = _df(opens, highs, lows, closes)
        det = StopHuntDetector()
        obs = det.detect("TEST", bars)
        assert any(o.features.get("direction") == "bull" for o in obs)
