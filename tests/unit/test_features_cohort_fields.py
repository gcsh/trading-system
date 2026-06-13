"""MITS Phase 14.E — build_features surfaces cohort sample size + CI bounds + posterior."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.bot.features import build_features


pytestmark = [pytest.mark.unit]


def _snap_with_pattern():
    return {
        "price": 100.0,
        "rsi": 60.0,
        "ticker": "NVDA",
        "pattern": "bull_flag",
        "regime": "trending_up",
        "direction": "LONG",
    }


def test_cohort_fields_all_none_when_no_cohort():
    feats = build_features({"price": 100.0, "rsi": 60.0})
    for key in ("cohort_sample_size", "cohort_ci_lower",
                  "cohort_ci_upper", "cohort_posterior",
                  "cohort_ci_width"):
        assert key in feats
        assert feats[key] is None


def test_cohort_fields_populated_from_entry():
    entry = {
        "n": 120,
        "posterior": 0.68,
        "confidence_lower": 0.60, "confidence_upper": 0.76,
        "confidence_lower_long": 0.62, "confidence_upper_long": 0.74,
        "ci_width": 0.16,
    }
    with patch(
        "backend.bot.corpus.knowledge_graph.get_posterior_with_fallback",
        return_value=entry,
    ):
        feats = build_features(_snap_with_pattern())
    assert feats["cohort_sample_size"] == 120
    assert feats["cohort_posterior"] == pytest.approx(0.68, abs=1e-3)
    # Direction-specific bounds win.
    assert feats["cohort_ci_lower"] == pytest.approx(0.62, abs=1e-3)
    assert feats["cohort_ci_upper"] == pytest.approx(0.74, abs=1e-3)
    assert feats["cohort_ci_width"] == pytest.approx(0.12, abs=1e-3)


def test_cohort_falls_back_to_overall_when_direction_null():
    entry = {
        "n": 50,
        "posterior": 0.55,
        "confidence_lower": 0.45, "confidence_upper": 0.65,
        "confidence_lower_long": None, "confidence_upper_long": None,
    }
    with patch(
        "backend.bot.corpus.knowledge_graph.get_posterior_with_fallback",
        return_value=entry,
    ):
        feats = build_features(_snap_with_pattern())
    assert feats["cohort_sample_size"] == 50
    assert feats["cohort_ci_lower"] == pytest.approx(0.45, abs=1e-3)
    assert feats["cohort_ci_upper"] == pytest.approx(0.65, abs=1e-3)


def test_cohort_posterior_falls_back_to_posterior_win_rate_key():
    """Some callers populate ``posterior_win_rate`` instead of
    ``posterior``. The features helper should read either."""
    entry = {
        "n": 40,
        "posterior_win_rate": 0.72,
        "confidence_lower": 0.65, "confidence_upper": 0.79,
    }
    with patch(
        "backend.bot.corpus.knowledge_graph.get_posterior_with_fallback",
        return_value=entry,
    ):
        feats = build_features(_snap_with_pattern())
    assert feats["cohort_posterior"] == pytest.approx(0.72, abs=1e-3)


def test_other_features_still_present_alongside_new_cohort_fields():
    """Sanity guard — original feature keys are untouched."""
    feats = build_features({
        "price": 100.0, "rsi": 60.0, "macd": 0.5,
        "macd_signal": 0.3, "volume": 200, "avg_volume": 150,
    })
    for k in ("rsi_14", "macd_hist", "volume_ratio", "composite_bias",
                "cohort_ci_width"):
        assert k in feats
