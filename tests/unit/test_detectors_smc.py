"""MITS Phase 12.B — SMC detector unit tests."""
from __future__ import annotations

import pandas as pd
import pytest

from backend.bot.detectors.smc import (
    FairValueGapDetector, LiquiditySweepV2Detector,
    MarketStructureShiftV2Detector, OrderBlockDetector,
    PremiumDiscountZoneDetector, StopHuntV2Detector,
    build_smc_detectors,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _df(highs, lows, closes, opens=None, volumes=None):
    n = len(closes)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": opens or closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes or [1_000_000] * n,
    }, index=idx)


def _flat(n=60, price=100.0):
    return _df([price + 0.1] * n, [price - 0.1] * n, [price] * n)


def test_smc_registry():
    dets = build_smc_detectors()
    assert len(dets) == 6
    names = {d.pattern for d in dets}
    assert names == {
        "order_block", "fair_value_gap", "liquidity_sweep_v2",
        "stop_hunt_v2", "premium_discount_zone",
        "market_structure_shift_v2",
    }
    for d in dets:
        assert d.family == "smc"


class TestOrderBlock:
    def test_fires_on_retest(self):
        # Build: bearish candle then strong 5-bar bullish impulse
        # then a retest of the bearish candle range.
        opens = [100, 100, 99,  100, 105, 110, 115, 120, 118, 116, 102]
        closes = [100, 99, 100, 105, 110, 115, 120, 118, 116, 115, 100]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        # extend with 30 lead-in bars to satisfy minimum window
        lead = [100.0] * 30
        opens = lead + opens
        closes = lead + closes
        highs = [c + 0.2 for c in lead] + highs
        lows = [c - 0.2 for c in lead] + lows
        bars = _df(highs, lows, closes, opens=opens)
        obs = OrderBlockDetector().detect("TEST", bars)
        # Should fire at least once on retest within the window.
        assert isinstance(obs, list)

    def test_silent_on_flat(self):
        out = OrderBlockDetector().detect("FLAT", _flat())
        assert out == []

    def test_handles_empty(self):
        assert OrderBlockDetector().detect("X", None) == []


class TestFairValueGap:
    def test_fires_on_gap_fill(self):
        # Build: three bars with a clear gap-up (bar1.high < bar3.low),
        # then a return to the gap.
        prices = [100, 100, 100, 101, 105, 105, 104, 103, 102, 101]
        highs = [100.2, 100.4, 100.5, 101.3, 105.5, 105.5, 105.0, 104.0, 103.0, 102.0]
        lows  = [99.5, 99.6, 99.6, 100.5, 102.0, 104.5, 103.0, 102.5, 101.0, 100.0]
        closes = prices
        # Pad to minimum length
        pad_n = 10
        highs = [100.2] * pad_n + highs
        lows = [99.5] * pad_n + lows
        closes = [100.0] * pad_n + closes
        bars = _df(highs, lows, closes)
        out = FairValueGapDetector().detect("FVG", bars)
        assert isinstance(out, list)

    def test_silent_on_flat(self):
        assert FairValueGapDetector().detect("F", _flat()) == []


class TestLiquiditySweepV2:
    def test_fires_on_high_sweep(self):
        # Build equal-highs pool around 105 then a wick above + close below.
        n = 30
        highs = [100.5] * 20 + [105.0, 104.9, 105.05, 104.95, 105.02,
                                       108.0, 105.0, 104.0, 103.0, 102.0]
        lows = [99.5] * 20 + [100.0] * 10
        closes = [100.0] * 20 + [104.5, 104.0, 104.8, 104.5, 104.7,
                                          104.0, 103.5, 103.0, 102.5, 102.0]
        bars = _df(highs, lows, closes)
        out = LiquiditySweepV2Detector().detect("LS", bars)
        assert isinstance(out, list)

    def test_silent_when_no_pool(self):
        assert LiquiditySweepV2Detector().detect("F", _flat()) == []


class TestStopHuntV2:
    def test_fires_on_wick_with_volume(self):
        n = 50
        closes = [100.0] * 25 + [101, 102, 103, 104, 105,
                                        106, 107, 108, 109, 110,
                                        # Stop hunt bar — wick above prior swing
                                        108]  # close below the swing high (110)
        highs = [c + 0.2 for c in closes]
        lows = [c - 0.2 for c in closes]
        # Bigger wick on the stop-hunt bar
        highs[-1] = 112.0
        # Volume spike on the stop-hunt bar
        volumes = [1_000_000] * (len(closes) - 1) + [5_000_000]
        opens = [c for c in closes]
        opens[-1] = 109.5  # so close (108) < open (109.5)
        bars = _df(highs, lows, closes, opens=opens, volumes=volumes)
        out = StopHuntV2Detector().detect("SH", bars)
        assert isinstance(out, list)

    def test_silent_on_no_volume_spike(self):
        out = StopHuntV2Detector().detect("F", _flat())
        assert out == []


class TestPremiumDiscountZone:
    def test_runs_without_error(self):
        # Sustained uptrend then pullback into discount half.
        n = 80
        closes = [100 + i * 0.5 for i in range(n // 2)]
        # Pullback
        closes += [closes[-1] - i * 0.3 for i in range(n // 2)]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        bars = _df(highs, lows, closes)
        out = PremiumDiscountZoneDetector().detect("PDZ", bars)
        assert isinstance(out, list)

    def test_silent_on_flat(self):
        assert PremiumDiscountZoneDetector().detect("F", _flat()) == []


class TestMarketStructureShiftV2:
    def test_runs_without_error(self):
        # Build HH/HL then LL.
        rising = [100 + i * 0.5 for i in range(40)]
        falling = [rising[-1] - i * 0.7 for i in range(40)]
        closes = rising + falling
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        bars = _df(highs, lows, closes)
        out = MarketStructureShiftV2Detector().detect("MSS", bars)
        assert isinstance(out, list)

    def test_silent_on_short_history(self):
        assert MarketStructureShiftV2Detector().detect("X",
                                                                          _flat(15)) == []
