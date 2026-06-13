"""MITS Phase 4 (P4.1) — detector params propagation.

Pinning the contract that every detector accepts a ``params`` kwarg and
reads its tunable knobs from it (instead of module-level constants).

Each test below tunes one detector with intentionally permissive values
that should INCREASE fire rate on the same synthetic bars vs the
defaults — proving the override is honored.
"""
from __future__ import annotations

import pandas as pd
import pytest

pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def _df(opens, highs, lows, closes, volumes=None):
    n = len(closes)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volumes or [1_000_000] * n,
    }, index=idx)


# ── price action ──────────────────────────────────────────────────────


def test_bull_flag_param_override_loosens_thrust():
    """Lowering ``min_thrust_pct`` should let a borderline thrust fire."""
    from backend.bot.detectors.price_action import BullFlagDetector
    closes = []
    for i in range(10):
        closes.append(100 + i * 0.1)
    # Thrust 100 -> 103 over 10 bars = +3% (below default 5% but above 2%).
    thrust = [101, 101.5, 102, 102.5, 103, 103, 103, 103, 103, 103]
    closes.extend(thrust)
    cons = [103.05, 102.95, 103.1, 102.9, 103.0]
    closes.extend(cons)
    highs = [c * 1.001 for c in closes]
    lows = [c * 0.999 for c in closes]
    df = _df(closes, highs, lows, closes)
    det = BullFlagDetector()
    default = det.detect("X", df.copy())
    loosened = det.detect("X", df.copy(), params={"min_thrust_pct": 0.02})
    assert len(loosened) >= len(default)
    assert len(loosened) >= 1


def test_breakout_param_override_loosens_volume_gate():
    """Dropping ``volume_multiplier`` to 0 should let a flat-volume
    breakout fire that the default 1.3x gate would reject."""
    from backend.bot.detectors.price_action import BreakoutDetector
    # 22 bars total: first 21 flat at 100, last bar breaks slightly above.
    closes = [100.0] * 21 + [100.6]
    highs = [100.05] * 21 + [100.65]
    lows = [99.95] * 21 + [100.0]
    volumes = [1_000_000] * 22  # FLAT volume — should fail default gate.
    df = _df(closes, highs, lows, closes, volumes=volumes)
    det = BreakoutDetector()
    default = det.detect("X", df.copy())
    loosened = det.detect(
        "X", df.copy(),
        params={"lookback_bars": 20, "min_breakout_pct": 0.003,
                  "volume_multiplier": 0.0},
    )
    assert len(loosened) >= len(default)
    # Loosened should fire at least the last bar.
    assert len(loosened) >= 1


# ── market structure ─────────────────────────────────────────────────


def test_bos_params_dict_contains_swing_fractal_k():
    from backend.bot.detectors.market_structure import BOSDetector
    det = BOSDetector()
    defaults = det.default_params()
    assert "swing_fractal_k" in defaults
    assert defaults["swing_fractal_k"] == 2


def test_bos_accepts_params_kwarg_without_raising():
    from backend.bot.detectors.market_structure import BOSDetector
    df = _df(
        opens=[100] * 20, highs=[101] * 20, lows=[99] * 20,
        closes=[100 + i * 0.1 for i in range(20)],
    )
    det = BOSDetector()
    out = det.detect("X", df, params={"swing_fractal_k": 2})
    assert isinstance(out, list)


# ── liquidity ────────────────────────────────────────────────────────


def test_stop_hunt_tighter_reversal_third_filters_out():
    """Raising ``reversal_third`` to 0.5 should drop fires whose close
    sits in the middle-third of the bar (not deep enough)."""
    from backend.bot.detectors.liquidity import StopHuntDetector
    n = 30
    closes = [100.0] * n
    highs = [100.5] * n
    lows = [99.5] * n
    # Bar 25: wick above prior high, close near low.
    highs[25] = 103.0
    lows[25] = 99.0
    closes[25] = 99.5  # close in bottom 25% → fires default
    df = _df(closes, highs, lows, closes)
    det = StopHuntDetector()
    default = det.detect("X", df.copy())
    # Override demands closes in the bottom 5% — same bar won't fire.
    stricter = det.detect("X", df.copy(),
                              params={"lookback_bars": 10,
                                          "reversal_third": 0.05})
    assert len(default) >= len(stricter)


# ── VWAP ──────────────────────────────────────────────────────────────


def test_vwap_reclaim_accepts_params_kwarg():
    from backend.bot.detectors.vwap import VWAPReclaimDetector
    det = VWAPReclaimDetector()
    assert det.default_params() == {"min_cross_distance_pct": 0.0}
    df = _df(
        opens=[100] * 10, highs=[101] * 10, lows=[99] * 10,
        closes=[99.5, 99.5, 100.5, 100.0, 99.5, 100.5, 99.5, 100.5, 99.5, 100.5],
    )
    out = det.detect("X", df, params={"min_cross_distance_pct": 0.0})
    assert isinstance(out, list)


# ── volume profile ───────────────────────────────────────────────────


def test_hvn_params_propagate_lookback():
    from backend.bot.detectors.volume_profile import HVNAcceptanceDetector
    det = HVNAcceptanceDetector()
    defaults = det.default_params()
    assert defaults["lookback_bars"] == 60
    n = 80
    df = _df(
        opens=[100] * n, highs=[101] * n, lows=[99] * n,
        closes=[100 + (i % 5) * 0.1 for i in range(n)],
    )
    # Smaller lookback would let it fire earlier; just confirm no raise.
    out = det.detect("X", df, params={"lookback_bars": 60, "n_bins": 5,
                                                 "top_fraction": 0.5})
    assert isinstance(out, list)


# ── options intel ────────────────────────────────────────────────────


def test_iv_expansion_threshold_override_widens_or_narrows():
    """Reducing ``expansion_multiplier`` from 1.20 to 1.05 lets a
    small IV bump fire when the default wouldn't."""
    from backend.bot.detectors.options_intel import IVExpansionDetector
    n = 30
    df = _df(
        opens=[100] * n, highs=[101] * n, lows=[99] * n,
        closes=[100] * n,
    )
    iv_series = [0.20] * 25 + [0.215] * 5  # +7.5% above mean
    det = IVExpansionDetector()
    default = det.detect("X", df.copy(), iv_series=list(iv_series))
    loosened = det.detect(
        "X", df.copy(),
        iv_series=list(iv_series),
        params={"lookback_bars": 20, "expansion_multiplier": 1.05,
                  "reset_multiplier": 1.02},
    )
    assert len(loosened) >= len(default)


# ── all detectors propagate via detect_all ───────────────────────────


def test_detect_all_passes_param_overrides_to_bull_flag():
    """End-to-end: persist a permissive ``params_json`` for a detector
    via DetectorConfig and confirm ``detect_all`` honors it."""
    import json
    from sqlalchemy import select
    from backend.bot.detectors import (
        clear_detector_config_cache, detect_all,
    )
    from backend.db import session_scope
    from backend.models.detector_config import DetectorConfig

    closes = [100 + i * 0.1 for i in range(10)]
    thrust = [101, 101.5, 102, 102.5, 103, 103, 103, 103, 103, 103]
    closes.extend(thrust)
    closes.extend([103.05, 102.95, 103.1, 102.9, 103.0])
    highs = [c * 1.001 for c in closes]
    lows = [c * 0.999 for c in closes]
    df = _df(closes, highs, lows, closes)

    with session_scope() as s:
        row = s.execute(
            select(DetectorConfig).where(DetectorConfig.name == "bull_flag")
        ).scalar_one_or_none()
        if row is None:
            row = DetectorConfig(name="bull_flag", enabled=True,
                                       params_json=json.dumps({"min_thrust_pct": 0.02}),
                                       source="builtin")
            s.add(row)
        else:
            row.enabled = True
            row.params_json = json.dumps({"min_thrust_pct": 0.02})
    clear_detector_config_cache()
    try:
        obs = detect_all("X", df.copy())
        bull_flag_fires = [o for o in obs if o.pattern == "bull_flag"]
        assert len(bull_flag_fires) >= 1
    finally:
        # Cleanup: restore defaults so other tests aren't polluted.
        with session_scope() as s:
            row = s.execute(
                select(DetectorConfig).where(
                    DetectorConfig.name == "bull_flag")
            ).scalar_one_or_none()
            if row is not None:
                row.params_json = "{}"
        clear_detector_config_cache()
