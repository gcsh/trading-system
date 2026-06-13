"""MITS Phase 12.D — Volume Profile v2 detector unit tests."""
from __future__ import annotations

import pandas as pd
import pytest

from backend.bot.detectors.volume_profile_v2 import (
    CompositeValueAreaDetector, POCRetestDetector,
    ValueAreaRejectionDetector, _compute_profile,
    build_volume_profile_v2_detectors,
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


def test_vp_v2_registry():
    dets = build_volume_profile_v2_detectors()
    assert len(dets) == 3
    names = {d.pattern for d in dets}
    assert names == {"poc_retest", "value_area_rejection",
                            "composite_value_area"}
    for d in dets:
        assert d.family == "volume_profile_v2"


def test_compute_profile_basic():
    highs = [101] * 20
    lows = [99] * 20
    volumes = [1_000_000] * 20
    out = _compute_profile(highs, lows, volumes, 0, 20, bins=20)
    assert out is not None
    poc, val, vah, hist, bin_size = out
    assert 99 <= poc <= 101
    assert val <= poc <= vah


def test_compute_profile_zero_range_returns_none():
    out = _compute_profile([100.0] * 20, [100.0] * 20,
                                       [1_000_000] * 20, 0, 20)
    assert out is None


class TestPOCRetest:
    def test_runs_without_error(self):
        n = 60
        closes = [100 + (i % 5) * 0.5 for i in range(40)]
        # Excursion +5 then return
        closes += [105, 106, 105, 103, 101, 100, 99.95, 99.5, 100]
        closes += [100] * (n - len(closes))
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        bars = _df(highs, lows, closes)
        out = POCRetestDetector().detect("POC", bars)
        assert isinstance(out, list)

    def test_silent_on_flat(self):
        assert POCRetestDetector().detect("F", _flat()) == []


class TestValueAreaRejection:
    def test_runs_without_error(self):
        n = 40
        # Concentrated volume around 100 with occasional excursions.
        closes = [100, 100.1, 99.9, 101, 99,
                       100.2, 99.8, 105, 102, 100,
                       100.5, 99.5, 103, 100, 99,
                       100, 100.1, 99.9, 100.2, 99.8]
        closes = closes * 2
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        opens = closes
        # Make the last bar a rejection at VAL.
        highs[-1] = 101.0
        lows[-1] = 95.0
        opens[-1] = 96.0
        closes[-1] = 100.5
        bars = _df(highs, lows, closes, opens=opens)
        out = ValueAreaRejectionDetector().detect("VAR", bars)
        assert isinstance(out, list)


class TestCompositeValueArea:
    def test_runs_without_error(self):
        n = 100
        # Steady price with overlapping value areas.
        closes = [100 + (i % 5) * 0.2 for i in range(n)]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        bars = _df(highs, lows, closes)
        out = CompositeValueAreaDetector().detect("CVA", bars)
        assert isinstance(out, list)
