"""MITS Phase 14.E — operator-readable grade explainer."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from backend.bot.ranker import (
    _build_grade_explainer,
    build_grade_explainer_for_cohort,
    rank_trade,
)


pytestmark = [pytest.mark.unit]


def _probability(direction="LONG", probability=0.7, rr=2.0):
    return SimpleNamespace(
        direction=direction, probability=probability,
        risk_reward=rr, confidence=0.6,
    )


def _regime(trend="bullish", volatility="normal"):
    return SimpleNamespace(trend=trend, volatility=volatility)


def _confluence(direction="bullish", score=0.6):
    return SimpleNamespace(
        direction=direction, score=score, conflicting_timeframes=[],
    )


def _features(**overrides):
    base = {
        "flow_bullishness": 0.5,
        "volume_ratio": 1.2,
        "pinning_probability": 0.0,
        "dominant_wall": "neutral",
        "cohort_sample_size": 120,
        "cohort_ci_lower": 0.60,
        "cohort_ci_upper": 0.74,
        "cohort_posterior": 0.67,
    }
    base.update(overrides)
    return base


def test_explainer_leads_with_grade_letter():
    out = rank_trade(_probability(), _regime(), _confluence(), _features())
    assert out.grade_explainer.startswith(f"This is a {out.grade}")
    assert re.match(r"^This is a [A-CR]\+?\b", out.grade_explainer)


def test_explainer_quotes_cohort_when_present():
    out = rank_trade(_probability(), _regime(), _confluence(), _features())
    assert "cohort posterior is 67%" in out.grade_explainer
    assert "N=120" in out.grade_explainer
    assert "CI [60%, 74%]" in out.grade_explainer


def test_explainer_mentions_regime_alignment_when_aligned():
    out = rank_trade(_probability(direction="LONG"),
                          _regime(trend="bullish"),
                          _confluence(), _features())
    assert "bullish regime aligns with the trade" in out.grade_explainer


def test_explainer_calls_out_pin_headwind():
    feats = _features(pinning_probability=0.75, dominant_wall="call")
    out = rank_trade(_probability(direction="LONG"),
                          _regime(), _confluence(), feats)
    assert "call wall" in out.grade_explainer
    assert "pin probability 75%" in out.grade_explainer
    assert "edge eraser" in out.grade_explainer


def test_explainer_omits_pin_phrase_when_pin_low():
    feats = _features(pinning_probability=0.2, dominant_wall="call")
    out = rank_trade(_probability(direction="LONG"),
                          _regime(), _confluence(), feats)
    assert "wall" not in out.grade_explainer.lower()


def test_explainer_notes_wide_ci_discount():
    feats = _features(cohort_ci_lower=0.45, cohort_ci_upper=0.85)
    feats["cohort_ci_width"] = 0.40
    out = rank_trade(_probability(), _regime(), _confluence(), feats)
    assert "ci_penalty" in out.components
    assert "wide enough to discount" in out.grade_explainer


def test_explainer_no_ci_penalty_phrase_when_narrow():
    feats = _features()
    feats["cohort_ci_width"] = 0.05
    out = rank_trade(_probability(), _regime(), _confluence(), feats)
    assert "wide enough to discount" not in out.grade_explainer


def test_explainer_handles_missing_cohort_gracefully():
    feats = _features(cohort_sample_size=0, cohort_ci_lower=None,
                              cohort_ci_upper=None, cohort_posterior=None)
    out = rank_trade(_probability(probability=0.6),
                          _regime(), _confluence(), feats)
    assert out.grade_explainer.startswith(f"This is a {out.grade}")
    # With no cohort N + bounds we still surface the model-blended
    # probability so the operator gets *something*.
    assert (
        "model-blended probability" in out.grade_explainer
        or "no cohort match" in out.grade_explainer
    )


def test_standalone_helper_matches_rank_trade_shape():
    """The standalone helper used by the analysis route emits prose
    that matches the same lead-in pattern as the inline call."""
    text = build_grade_explainer_for_cohort(
        posterior=0.7, sample_size=80,
        ci_lower=0.60, ci_upper=0.78,
        regime_label="bullish",
        pinning_probability=0.0,
        grade="A", score=0.70,
        direction="LONG",
    )
    assert text.startswith("This is a A")
    assert "cohort posterior is 70%" in text
    assert "N=80" in text
    assert "CI [60%, 78%]" in text


def test_standalone_helper_calls_out_pin_when_aimed_at_wall():
    text = build_grade_explainer_for_cohort(
        posterior=0.65, sample_size=50,
        ci_lower=0.55, ci_upper=0.75,
        regime_label="bullish",
        pinning_probability=0.7,
        grade="B", score=0.65,
        direction="LONG", dominant_wall="call",
    )
    assert "call wall" in text
    assert "pin probability 70%" in text


def test_explainer_call_paths_share_internal_function():
    """Sanity: the wired function and the standalone share their inner
    composer."""
    inline = _build_grade_explainer(
        grade="A", score=0.72,
        comps={"regime": 0.85},
        probability=_probability(probability=0.72),
        regime=_regime(),
        features={
            "cohort_sample_size": 90, "cohort_posterior": 0.70,
            "cohort_ci_lower": 0.60, "cohort_ci_upper": 0.78,
            "pinning_probability": 0.0, "dominant_wall": "neutral",
        },
    )
    assert inline.startswith("This is a A")
    assert "CI [60%, 78%]" in inline
