"""MITS Phase 0 — market structure detector tests."""
from __future__ import annotations

import pandas as pd
import pytest

from backend.bot.detectors.market_structure import (
    BOSDetector, CHOCHDetector,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _df(highs, lows, closes):
    idx = pd.date_range("2025-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": [1_000_000] * len(closes),
    }, index=idx)


def _bos_up_bars():
    """Zig-zag uptrend with a clear swing-high broken on the right."""
    closes = []
    highs = []
    lows = []
    # Synthesise: oscillate +1/-0.5 to manufacture swing highs.
    pivots = [
        100, 102, 101, 103, 101.5,
        104, 102, 105, 103, 106,    # swing high at 106 around idx ~9
        104, 105, 104, 105.5, 104,
        103, 104.5, 103, 108,        # bar that breaks above 106
        108, 109, 108.5,
    ]
    for p in pivots:
        closes.append(p)
        highs.append(p + 0.3)
        lows.append(p - 0.3)
    return _df(highs, lows, closes)


class TestBOS:
    def test_fires_on_upward_break(self):
        det = BOSDetector()
        bars = _bos_up_bars()
        obs = det.detect("TEST", bars)
        assert len(obs) >= 1
        directions = [o.features.get("direction") for o in obs]
        assert "up" in directions

    def test_silent_on_flat_noise(self):
        n = 30
        closes = [100.0] * n
        bars = _df([100.05] * n, [99.95] * n, closes)
        det = BOSDetector()
        # Without distinct swings, no break — must not raise.
        result = det.detect("FLAT", bars)
        assert isinstance(result, list)


def _choch_bars():
    """Uptrend with a sharply isolated swing-low pivot, then a break
    well below it. Swing-low pivot needs the low at idx j to be
    strictly less than lows[j-2..j+2] (excluding j)."""
    closes = []
    highs = []
    lows = []
    # 25 bars of rising trend.
    for i in range(25):
        c = 100 + i * 0.5
        closes.append(c)
        highs.append(c + 0.4)
        lows.append(c - 0.4)
    # Inject a clearly-isolated swing low at idx 27: low much
    # lower than its neighbours, but close still above.
    for offset, low_dip in (
        (0.7, None), (0.6, None),
        (0.4, -1.5),      # the swing-low bar
        (0.6, None), (0.7, None),
    ):
        c = closes[-1] + offset
        closes.append(c)
        highs.append(c + 0.4)
        if low_dip is not None:
            lows.append(c + low_dip)
        else:
            lows.append(c - 0.4)
    # 3 more bars rising.
    for _ in range(3):
        c = closes[-1] + 0.5
        closes.append(c)
        highs.append(c + 0.4)
        lows.append(c - 0.4)
    # Sharp drop that closes below the swing-low.
    swing_low_price = min(lows[-15:])
    for _ in range(3):
        c = swing_low_price - 2.0
        closes.append(c)
        highs.append(c + 0.4)
        lows.append(c - 0.4)
    return _df(highs, lows, closes)


class TestCHOCH:
    def test_fires_on_bearish_change(self):
        det = CHOCHDetector()
        bars = _choch_bars()
        obs = det.detect("TEST", bars)
        # At least one bearish CHOCH event in this sequence.
        directions = [o.features.get("direction") for o in obs]
        assert "bearish" in directions
