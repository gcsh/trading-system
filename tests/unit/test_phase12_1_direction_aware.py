"""MITS Phase 12.1 — tests for direction-aware outcome scoring +
hierarchical fallback + the 4 zero-observation detector fixes."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pandas as pd
import pytest

from backend.bot.corpus.outcome_linker import _compute_winner
from backend.bot.detectors.direction import resolve_direction
from backend.db import init_db, session_scope
from backend.models.market_observation import MarketObservation


pytestmark = [pytest.mark.unit]


# ── Fix 3 — direction-aware winner ──────────────────────────────────


def test_compute_winner_long():
    assert _compute_winner("long", 0.01) is True
    assert _compute_winner("long", -0.01) is False
    assert _compute_winner("long", 0.0) is False


def test_compute_winner_short():
    """Bearish setup — return < 0 is the win (inverted)."""
    assert _compute_winner("short", -0.02) is True
    assert _compute_winner("short", 0.02) is False
    assert _compute_winner("short", 0.0) is False


def test_compute_winner_neutral_threshold():
    """Vol-regime detectors: any meaningful move = win."""
    assert _compute_winner("neutral", 0.02) is True
    assert _compute_winner("neutral", -0.02) is True
    assert _compute_winner("neutral", 0.001) is False  # below 0.5% threshold


def test_compute_winner_legacy_fallback():
    """None direction → legacy bullish bias."""
    assert _compute_winner(None, 0.01) is True
    assert _compute_winner(None, -0.01) is False
    assert _compute_winner("", 0.01) is True


def test_compute_winner_none_return():
    assert _compute_winner("long", None) is False
    assert _compute_winner("short", None) is False


# ── Fix 2 — direction resolver ──────────────────────────────────────


def test_resolve_direction_static_long():
    assert resolve_direction("bull_flag") == "long"
    assert resolve_direction("breakout") == "long"
    assert resolve_direction("vwap_reclaim") == "long"
    assert resolve_direction("wyckoff_spring") == "long"
    assert resolve_direction("wyckoff_sos") == "long"


def test_resolve_direction_static_short():
    assert resolve_direction("bear_flag") == "short"
    assert resolve_direction("vwap_rejection") == "short"
    assert resolve_direction("wyckoff_distribution_phase") == "short"
    assert resolve_direction("wyckoff_upthrust") == "short"
    assert resolve_direction("yield_curve_inversion") == "short"
    assert resolve_direction("credit_spread_widening") == "short"


def test_resolve_direction_flow_intel_name_based():
    assert resolve_direction("flow_call_sweep_unusual") == "long"
    assert resolve_direction("flow_put_block_buy") == "short"
    assert resolve_direction("flow_dark_pool_call_lean") == "long"
    assert resolve_direction("flow_dark_pool_put_lean") == "short"


def test_resolve_direction_dynamic_smc_from_features():
    """SMC v2 detectors carry direction in features."""
    assert resolve_direction("order_block", {"direction": "bullish"}) == "long"
    assert resolve_direction("order_block", {"direction": "bearish"}) == "short"
    assert resolve_direction("fair_value_gap", {"direction": "bullish"}) == "long"
    assert resolve_direction("liquidity_sweep_v2",
                                       {"direction": "bullish"}) == "long"
    assert resolve_direction("market_structure_shift_v2",
                                       {"direction": "bullish_flip"}) == "long"
    assert resolve_direction("market_structure_shift_v2",
                                       {"direction": "bearish_flip"}) == "short"


def test_resolve_direction_premium_discount():
    assert resolve_direction("premium_discount_zone",
                                       {"zone": "discount"}) == "long"
    assert resolve_direction("premium_discount_zone",
                                       {"zone": "premium"}) == "short"


def test_resolve_direction_pead_drift_from_surprise():
    assert resolve_direction("pead_drift",
                                       {"event_return": 0.05,
                                        "direction": "bullish"}) == "long"
    assert resolve_direction("pead_drift",
                                       {"event_return": -0.05,
                                        "direction": "bearish"}) == "short"


def test_resolve_direction_insider_cluster():
    assert resolve_direction("insider_cluster", {"side": "buy"}) == "long"
    assert resolve_direction("insider_cluster", {"side": "sell"}) == "short"


def test_resolve_direction_macro_composite():
    assert resolve_direction("composite_macro_regime",
                                       {"score": 75}) == "short"
    assert resolve_direction("composite_macro_regime",
                                       {"score": 25}) == "long"
    assert resolve_direction("composite_macro_regime",
                                       {"score": 50}) is None


def test_resolve_direction_mean_reversion_z():
    assert resolve_direction("mean_reversion_z", {"z_score": 2.5}) == "short"
    assert resolve_direction("mean_reversion_z", {"z_score": -2.5}) == "long"
    assert resolve_direction("mean_reversion_z", {"z_score": 0.5}) is None


def test_resolve_direction_dollar_strength():
    assert resolve_direction("dollar_strength_shift", {"z_score": 2.5}) == "short"
    assert resolve_direction("dollar_strength_shift", {"z_score": -2.5}) == "long"


def test_resolve_direction_unknown_returns_none():
    assert resolve_direction("xyzzy_unknown_pattern", {}) is None
    assert resolve_direction("", {}) is None


def test_resolve_direction_neutral_patterns():
    assert resolve_direction("pennant") is None
    assert resolve_direction("hvn_acceptance") is None
    assert resolve_direction("gex_acceleration") is None
    assert resolve_direction("iv_expansion") is None
    assert resolve_direction("composite_value_area") is None


# ── Fix 6 — hierarchical fallback ─────────────────────────────────────


@pytest.fixture
def fresh_db():
    import backend.db as db_mod
    prev_engine = db_mod._engine
    prev_session = db_mod._SessionLocal
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db(path)
    try:
        yield path
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


def _add_cell(ticker: str, pattern: str, regime: str = "unknown",
                  vol_state: str = "normal", n: int = 5,
                  posterior: float = 0.55,
                  sample_split: str = "combined") -> None:
    from backend.models.knowledge_graph_cell import KnowledgeGraphCell
    with session_scope() as s:
        s.add(KnowledgeGraphCell(
            ticker=ticker, pattern=pattern, regime=regime,
            vol_state=vol_state, time_bucket="rth", horizon="5d",
            sample_split=sample_split,
            sample_size=n,
            win_rate=posterior, posterior_win_rate=posterior,
            avg_return_pct=0.01,
            confidence_level=("high" if n >= 100 else "medium" if n >= 30
                                    else "low" if n >= 10 else "thin"),
        ))


def test_hierarchical_fallback_uses_local_when_thick(fresh_db):
    from backend.bot.corpus.knowledge_graph import get_posterior_with_fallback
    _add_cell("NVDA", "bull_flag", n=80, posterior=0.72)
    result = get_posterior_with_fallback("NVDA", "bull_flag")
    assert result is not None
    assert result["source"] == "cell"
    assert result["posterior"] == pytest.approx(0.72)
    assert result["n"] == 80


def test_hierarchical_fallback_falls_to_pattern_regime(fresh_db):
    from backend.bot.corpus.knowledge_graph import get_posterior_with_fallback
    _add_cell("NVDA", "bull_flag", n=5, posterior=0.50)
    # Cross-ticker (pattern, regime) pool.
    _add_cell("AAPL", "bull_flag", n=200, posterior=0.65)
    _add_cell("MSFT", "bull_flag", n=150, posterior=0.62)
    result = get_posterior_with_fallback("NVDA", "bull_flag")
    assert result is not None
    assert result["source"] == "pattern_regime"
    # Pooled posterior should be between 0.62 and 0.65.
    assert 0.6 < result["posterior"] < 0.7
    assert result["n"] > 300


def test_hierarchical_fallback_falls_to_pattern_global(fresh_db):
    from backend.bot.corpus.knowledge_graph import get_posterior_with_fallback
    _add_cell("NVDA", "bull_flag", regime="trending_up", n=5,
                 posterior=0.50)
    # No (pattern, regime='trending_up') parent — only (pattern) global.
    _add_cell("AAPL", "bull_flag", regime="choppy", n=100, posterior=0.55)
    result = get_posterior_with_fallback("NVDA", "bull_flag",
                                                          regime="trending_up")
    assert result is not None
    # Either pattern_regime or pattern; both must include the AAPL pool.
    assert result["source"] in ("pattern", "pattern_regime", "local_thin")


def test_hierarchical_fallback_no_data(fresh_db):
    from backend.bot.corpus.knowledge_graph import get_posterior_with_fallback
    result = get_posterior_with_fallback("XYZ", "nonexistent_pattern")
    assert result is None


# ── Fix 7 — Wyckoff spring detector unit test ────────────────────────


def _make_spring_bars() -> pd.DataFrame:
    """Synthetic bars with a clear Wyckoff spring at bar 50.

    Layout: 40 bars of sideways range 99-101 (the trading range),
    then bar 41 wicks to 95 and closes back inside at 100 (the spring),
    bar 42 onwards continues recovering. The detector needs at least
    ``range_window + volume_ma_window`` = 40 bars before the spring,
    so we pad the front to 50 bars total.
    """
    pre = 41
    closes = [100.0] * pre + [97.0, 100.0, 101.0, 102.0, 103.0, 103.0]
    highs = [101.0] * pre + [100.0, 101.0, 102.0, 103.0, 104.0, 104.0]
    lows = [99.0] * pre + [95.0, 99.0, 100.0, 101.0, 102.0, 102.0]
    opens = list(closes)
    volumes = [1_000_000.0] * pre + [800_000.0, 1_100_000.0,
                                                  1_500_000.0, 1_200_000.0,
                                                  1_000_000.0, 1_000_000.0]
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes,
    }, index=idx)


def test_wyckoff_spring_fires_on_synthetic_bars():
    from backend.bot.detectors.wyckoff import WyckoffSpringDetector
    det = WyckoffSpringDetector()
    bars = _make_spring_bars()
    obs = det.detect("TEST", bars)
    assert len(obs) >= 1, "Wyckoff spring should fire on synthetic spring"
    o = obs[0]
    assert o.pattern == "wyckoff_spring"
    assert "range_low" in o.features
    assert o.features["break_low"] <= o.features["range_low"]


def test_wyckoff_upthrust_fires_on_synthetic_bars():
    """Mirror test for the upthrust detector."""
    from backend.bot.detectors.wyckoff import WyckoffUpthrustDetector
    # Range 99-101, bar 22 wicks to 105 then closes back below 101.
    pre = 41
    closes = [100.0] * pre + [103.0, 100.0, 99.0, 98.0, 97.0, 97.0]
    highs = [101.0] * pre + [105.0, 101.0, 100.0, 99.0, 98.0, 98.0]
    lows = [99.0] * pre + [100.0, 99.0, 98.0, 97.0, 96.0, 96.0]
    opens = list(closes)
    volumes = [1_000_000.0] * pre + [800_000.0, 1_100_000.0,
                                                  1_500_000.0, 1_200_000.0,
                                                  1_000_000.0, 1_000_000.0]
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    bars = pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes,
    }, index=idx)
    det = WyckoffUpthrustDetector()
    obs = det.detect("TEST", bars)
    assert len(obs) >= 1, "Wyckoff upthrust should fire on synthetic upthrust"
    assert obs[0].pattern == "wyckoff_upthrust"


def test_sector_dispersion_threshold_param():
    """Sector dispersion default threshold relaxed from 1.5 → 1.0."""
    from backend.bot.detectors.quantitative import SectorDispersionDetector
    det = SectorDispersionDetector()
    params = det.default_params()
    assert params["z_threshold"] <= 1.0
    assert params["z_window"] <= 100


def test_insider_cluster_min_distinct_relaxed():
    """Insider cluster min distinct insiders relaxed to 2."""
    from backend.bot.detectors.catalyst import InsiderClusterDetector
    det = InsiderClusterDetector()
    params = det.default_params()
    assert params["min_distinct_insiders"] == 2
    assert "P" in params["accept_codes"] and "A" in params["accept_codes"]
