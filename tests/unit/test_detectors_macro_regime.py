"""MITS Phase 12.F — Macro regime detector unit tests."""
from __future__ import annotations

import pandas as pd
import pytest

from backend.bot.detectors.macro_regime import (
    CANONICAL_MACRO_CARRIER, CompositeMacroRegimeDetector,
    CreditSpreadWideningDetector, DollarStrengthShiftDetector,
    YieldCurveInversionDetector, build_macro_regime_detectors,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _df(n=300, price=400.0):
    idx = pd.date_range("2023-01-01", periods=n, freq="D")
    closes = [price + i * 0.05 for i in range(n)]
    return pd.DataFrame({
        "open": closes, "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes], "close": closes,
        "volume": [1_000_000] * n,
    }, index=idx)


def test_macro_registry():
    dets = build_macro_regime_detectors()
    assert len(dets) == 4
    names = {d.pattern for d in dets}
    assert names == {
        "yield_curve_inversion", "credit_spread_widening",
        "dollar_strength_shift", "composite_macro_regime",
    }
    for d in dets:
        assert d.family == "macro_regime"


class TestYieldCurveInversion:
    def test_skips_non_carrier_ticker(self):
        out = YieldCurveInversionDetector().detect("AAPL", _df())
        # Non-SPY tickers must be silent — they don't carry macro.
        assert out == []

    def test_empty_fred_returns_empty(self):
        # On SPY but FRED tables are empty in test DB.
        out = YieldCurveInversionDetector().detect(
            CANONICAL_MACRO_CARRIER, _df())
        assert out == []


class TestCreditSpreadWidening:
    def test_skips_non_carrier_ticker(self):
        out = CreditSpreadWideningDetector().detect("MSFT", _df())
        assert out == []

    def test_handles_short_bars(self):
        out = CreditSpreadWideningDetector().detect(
            CANONICAL_MACRO_CARRIER, _df(5))
        assert out == []


class TestDollarStrengthShift:
    def test_skips_non_carrier_ticker(self):
        out = DollarStrengthShiftDetector().detect("NVDA", _df())
        assert out == []

    def test_empty_fred_returns_empty(self):
        out = DollarStrengthShiftDetector().detect(
            CANONICAL_MACRO_CARRIER, _df())
        assert out == []


class TestCompositeMacroRegime:
    def test_skips_non_carrier_ticker(self):
        out = CompositeMacroRegimeDetector().detect("META", _df())
        assert out == []

    def test_empty_fred_returns_empty(self):
        out = CompositeMacroRegimeDetector().detect(
            CANONICAL_MACRO_CARRIER, _df())
        assert out == []
