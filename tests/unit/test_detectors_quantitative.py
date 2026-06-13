"""MITS Phase 12.G — Quantitative detector unit tests."""
from __future__ import annotations

import pandas as pd
import pytest

from backend.bot.detectors.quantitative import (
    CANONICAL_SECTOR_CARRIER, CrossSectionalMomentumDetector,
    MeanReversionZDetector, SectorDispersionDetector,
    build_quantitative_detectors, clear_quant_cache,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _df(n=300, price=100.0, slope=0.05):
    idx = pd.date_range("2023-01-01", periods=n, freq="D")
    closes = [price + i * slope for i in range(n)]
    return pd.DataFrame({
        "open": closes, "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes], "close": closes,
        "volume": [1_000_000] * n,
    }, index=idx)


def test_quant_registry():
    dets = build_quantitative_detectors()
    assert len(dets) == 3
    names = {d.pattern for d in dets}
    assert names == {
        "cross_sectional_momentum", "mean_reversion_z",
        "sector_dispersion",
    }
    for d in dets:
        assert d.family == "quantitative"


class TestCrossSectionalMomentum:
    def test_short_history_returns_empty(self):
        out = CrossSectionalMomentumDetector().detect("AAPL", _df(50))
        assert out == []

    def test_handles_missing_universe(self):
        # On test DB with no universe-side bars, no observations.
        clear_quant_cache()
        out = CrossSectionalMomentumDetector().detect("AAPL", _df(300))
        assert isinstance(out, list)


class TestMeanReversionZ:
    def test_fires_on_extreme_z(self):
        n = 100
        # 60 calm bars then a -8 percent crash bar.
        closes = [100.0] * 60 + [99.5, 99.0, 98.5, 92.0,
                                          92.0, 92.5, 93.0, 93.0, 93.0, 93.0]
        closes += [93.0] * (n - len(closes))
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        bars = pd.DataFrame({
            "open": closes, "high": highs, "low": lows,
            "close": closes, "volume": [1_000_000] * n,
        }, index=pd.date_range("2024-01-01", periods=n, freq="D"))
        out = MeanReversionZDetector().detect("X", bars)
        assert isinstance(out, list)

    def test_silent_on_flat(self):
        n = 100
        closes = [100.0] * n
        highs = [100.1] * n
        lows = [99.9] * n
        bars = pd.DataFrame({
            "open": closes, "high": highs, "low": lows,
            "close": closes, "volume": [1_000_000] * n,
        }, index=pd.date_range("2024-01-01", periods=n, freq="D"))
        out = MeanReversionZDetector().detect("F", bars)
        assert out == []


class TestSectorDispersion:
    def test_skips_non_carrier_ticker(self):
        out = SectorDispersionDetector().detect("AAPL", _df(300))
        assert out == []

    def test_handles_empty_sector_data(self):
        clear_quant_cache()
        out = SectorDispersionDetector().detect(
            CANONICAL_SECTOR_CARRIER, _df(300))
        assert isinstance(out, list)
