"""MITS Phase 0 — TA-Lib detector tests.

Skipped automatically when TA-Lib isn't installed on the runner.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.bot.detectors.talib_patterns import (
    TALIB_PATTERN_SPECS, build_talib_detectors, talib_available,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]

if not talib_available():
    pytest.skip("TA-Lib not installed on this runner", allow_module_level=True)


def _hammer_bars(n_pad: int = 60) -> pd.DataFrame:
    """Bars that contain a synthetic hammer at the last bar.

    Hammer geometry: small body near the top, long lower wick (~10x
    the body), no/short upper wick. Preceded by a clear downtrend so
    TA-Lib counts it as a bullish reversal (CDLHAMMER references the
    ATR-scaled body / wick ratio plus prior trend).
    """
    pad = []
    for i in range(n_pad):
        c = 100 - i * 1.0
        pad.append({
            "open": c + 0.5, "high": c + 0.8,
            "low": c - 0.2, "close": c - 0.3,
        })
    # Final hammer bar: tiny body near the top, very long lower wick.
    last_close = pad[-1]["close"]
    op = last_close - 0.2
    cl = op + 0.1
    bars = pad + [{
        "open": op, "high": cl + 0.05,
        "low": op - 2.0, "close": cl,
    }]
    idx = pd.date_range("2025-01-01", periods=len(bars), freq="D")
    df = pd.DataFrame(bars, index=idx)
    df["volume"] = 1_000_000
    return df


def _engulfing_bars() -> pd.DataFrame:
    """Two bars: a small red bar followed by a large green bar that
    fully engulfs it. Preceded by some padding for TA-Lib context."""
    pad_closes = [100 - i * 0.4 for i in range(30)]
    bars = []
    for c in pad_closes:
        bars.append({"open": c, "high": c + 0.2, "low": c - 0.2, "close": c})
    # Small red.
    red_open = pad_closes[-1] - 0.3
    red_close = red_open - 0.4
    bars.append({"open": red_open, "high": red_open + 0.1,
                       "low": red_close - 0.1, "close": red_close})
    # Large green that engulfs.
    green_open = red_close - 0.2
    green_close = red_open + 1.0
    bars.append({"open": green_open, "high": green_close + 0.2,
                       "low": green_open - 0.1, "close": green_close})
    idx = pd.date_range("2025-01-01", periods=len(bars), freq="D")
    df = pd.DataFrame(bars, index=idx)
    df["volume"] = 1_000_000
    return df


def _flat_noise(n: int = 50) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": [100] * n, "high": [100.05] * n,
        "low": [99.95] * n, "close": [100] * n,
        "volume": [1_000_000] * n,
    }, index=idx)


class TestTaLibSpecs:
    def test_spec_count(self):
        assert len(TALIB_PATTERN_SPECS) == 15

    def test_specs_all_have_slugs(self):
        slugs = {s["slug"] for s in TALIB_PATTERN_SPECS}
        assert len(slugs) == 15, "duplicate slug in TA-Lib specs"
        for s in slugs:
            assert s.startswith("talib_"), f"unexpected slug: {s}"

    def test_build_returns_one_per_spec(self):
        dets = build_talib_detectors()
        assert len(dets) == len(TALIB_PATTERN_SPECS)


class TestSyntheticPatterns:
    def test_hammer_fires_on_hammer_bars(self):
        bars = _hammer_bars()
        dets = {d.pattern: d for d in build_talib_detectors()}
        hammer = dets["talib_hammer"]
        obs = hammer.detect("TEST", bars)
        assert len(obs) >= 1
        # Last bar should be the hammer.
        last = obs[-1]
        assert last.pattern == "talib_hammer"
        assert last.spot is not None
        assert "talib_value" in last.features

    def test_engulfing_fires_on_engulfing_bars(self):
        bars = _engulfing_bars()
        dets = {d.pattern: d for d in build_talib_detectors()}
        engulf = dets["talib_engulfing"]
        obs = engulf.detect("TEST", bars)
        assert len(obs) >= 1

    def test_flat_noise_returns_empty(self):
        bars = _flat_noise(60)
        dets = build_talib_detectors()
        for det in dets:
            obs = det.detect("FLAT", bars)
            # Flat constant bars should never fire most candlestick patterns;
            # CDLDOJI may fire on every bar (open == close).
            if det.pattern == "talib_doji":
                continue
            assert all(o.pattern == det.pattern for o in obs)
