"""MITS Phase 14.A — direction-aware cohort CI flows into build_features."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.bot.features import build_features


pytestmark = [pytest.mark.unit]


def _base_snapshot():
    return {
        "price": 100.0,
        "rsi": 60.0,
        "macd": 0.5,
        "macd_signal": 0.3,
        "volume": 200,
        "avg_volume": 150,
    }


def test_ci_width_none_when_snapshot_lacks_pattern():
    feats = build_features(_base_snapshot())
    assert "cohort_ci_width" in feats
    assert feats["cohort_ci_width"] is None


def test_ci_width_none_when_lookup_returns_nothing():
    snap = _base_snapshot()
    snap.update({"ticker": "NVDA", "pattern": "bull_flag",
                  "regime": "trending_up", "direction": "LONG"})
    with patch(
        "backend.bot.corpus.knowledge_graph.get_posterior_with_fallback",
        return_value=None,
    ):
        feats = build_features(snap)
    assert feats["cohort_ci_width"] is None


def test_long_uses_direction_specific_bounds_when_present():
    snap = _base_snapshot()
    snap.update({"ticker": "NVDA", "pattern": "bull_flag",
                  "regime": "trending_up", "direction": "LONG"})
    entry = {
        "confidence_lower": 0.55, "confidence_upper": 0.75,  # overall (0.20)
        "confidence_lower_long": 0.62, "confidence_upper_long": 0.70,  # long (0.08)
        "confidence_lower_short": 0.40, "confidence_upper_short": 0.78,  # short (0.38)
        "ci_width": 0.20,
    }
    with patch(
        "backend.bot.corpus.knowledge_graph.get_posterior_with_fallback",
        return_value=entry,
    ):
        feats = build_features(snap)
    assert feats["cohort_ci_width"] == pytest.approx(0.08, abs=1e-4)


def test_short_uses_short_specific_bounds():
    snap = _base_snapshot()
    snap.update({"ticker": "NVDA", "pattern": "bear_flag",
                  "regime": "trending_down", "direction": "SHORT"})
    entry = {
        "confidence_lower_long": 0.62, "confidence_upper_long": 0.70,
        "confidence_lower_short": 0.55, "confidence_upper_short": 0.65,  # short (0.10)
        "ci_width": 0.40,
    }
    with patch(
        "backend.bot.corpus.knowledge_graph.get_posterior_with_fallback",
        return_value=entry,
    ):
        feats = build_features(snap)
    assert feats["cohort_ci_width"] == pytest.approx(0.10, abs=1e-4)


def test_falls_back_to_overall_ci_when_direction_bounds_null():
    snap = _base_snapshot()
    snap.update({"ticker": "NVDA", "pattern": "bull_flag",
                  "regime": "trending_up", "direction": "LONG"})
    entry = {
        "confidence_lower_long": None, "confidence_upper_long": None,
        "confidence_lower_short": None, "confidence_upper_short": None,
        "ci_width": 0.22,
        "confidence_lower": 0.55, "confidence_upper": 0.77,
    }
    with patch(
        "backend.bot.corpus.knowledge_graph.get_posterior_with_fallback",
        return_value=entry,
    ):
        feats = build_features(snap)
    assert feats["cohort_ci_width"] == pytest.approx(0.22, abs=1e-4)


def test_overall_bounds_used_when_only_lower_upper_present():
    """No ci_width field; only confidence_lower/upper present."""
    snap = _base_snapshot()
    snap.update({"ticker": "NVDA", "pattern": "bull_flag",
                  "regime": "trending_up", "direction": "LONG"})
    entry = {
        "confidence_lower": 0.40, "confidence_upper": 0.60,
    }
    with patch(
        "backend.bot.corpus.knowledge_graph.get_posterior_with_fallback",
        return_value=entry,
    ):
        feats = build_features(snap)
    assert feats["cohort_ci_width"] == pytest.approx(0.20, abs=1e-4)


def test_no_direction_falls_back_to_overall():
    """Snapshot doesn't carry direction → use overall ci_width."""
    snap = _base_snapshot()
    snap.update({"ticker": "NVDA", "pattern": "bull_flag",
                  "regime": "trending_up"})
    entry = {
        "confidence_lower_long": 0.62, "confidence_upper_long": 0.70,
        "ci_width": 0.18,
    }
    with patch(
        "backend.bot.corpus.knowledge_graph.get_posterior_with_fallback",
        return_value=entry,
    ):
        feats = build_features(snap)
    assert feats["cohort_ci_width"] == pytest.approx(0.18, abs=1e-4)


def test_other_features_still_present_after_ci_addition():
    """Existing features must keep flowing — sanity guard."""
    feats = build_features(_base_snapshot())
    for k in ("rsi_14", "macd_hist", "volume_ratio", "composite_bias"):
        assert k in feats
