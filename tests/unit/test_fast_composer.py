"""MITS Phase 14.A — fast composer deterministic rules + CI handling."""
from __future__ import annotations

import math

import pytest

from backend.bot.analysis.fast_composer import (
    FastComposerResult,
    fast_compose_all,
    fast_compose_one,
)


pytestmark = [pytest.mark.unit]


def _cohort(*, post=0.6, n=100, lo=None, hi=None, ci_width=None,
              regime="trending_up", vol_state="normal",
              avg_return=0.02, avg_hold=60.0):
    out = {
        "posterior_win_rate": post,
        "sample_size": n,
        "regime": regime,
        "vol_state": vol_state,
        "avg_return_pct": avg_return,
        "avg_hold_minutes": avg_hold,
    }
    if lo is not None:
        out["confidence_lower"] = lo
    if hi is not None:
        out["confidence_upper"] = hi
    if ci_width is not None:
        out["ci_width"] = ci_width
    return out


def test_buy_call_when_bullish_above_min_posterior():
    r = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=0.65, n=200, lo=0.6, hi=0.7),
        spot=100.0,
    )
    assert r.action == "BUY_CALL"
    assert r.direction == "long_call"
    assert r.suggested_action is not None
    assert r.suggested_action["action"] == "BUY_CALL"


def test_buy_put_when_bearish_above_min_posterior():
    r = fast_compose_one(
        ticker="NVDA", pattern="bear_flag",
        cohort=_cohort(post=0.62, n=150, lo=0.55, hi=0.69),
        spot=100.0,
    )
    assert r.action == "BUY_PUT"
    assert r.direction == "long_put"


def test_skip_when_posterior_below_floor():
    r = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=0.40, n=200),
        spot=100.0,
    )
    assert r.action == "SKIP"
    assert r.suggested_action is None


def test_skip_when_pattern_is_neutral():
    r = fast_compose_one(
        ticker="NVDA", pattern="pennant",  # static map = None
        cohort=_cohort(post=0.70, n=200),
        spot=100.0,
    )
    assert r.action == "SKIP"
    assert r.direction == "neutral"


def test_suggested_action_gated_by_sample_size():
    r = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=0.7, n=10),
        spot=100.0,
    )
    assert r.action == "BUY_CALL"
    assert r.suggested_action is None


def test_rank_formula_matches_eod_rank_score_normalised():
    post, n = 0.65, 200
    r = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=post, n=n),
        spot=100.0,
    )
    expected = max(0.0, min(1.0, post * math.log1p(n) / 8.0))
    assert r.rank == pytest.approx(expected, abs=1e-4)


def test_uncertainty_wider_ci_yields_higher_uncertainty():
    narrow = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=0.65, n=200, lo=0.62, hi=0.68),
        spot=100.0,
    )
    wide = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=0.65, n=200, lo=0.45, hi=0.85),
        spot=100.0,
    )
    assert wide.uncertainty > narrow.uncertainty


def test_uncertainty_thin_sample_drives_signal_up():
    thin = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=0.65, n=20, lo=0.6, hi=0.7),
        spot=100.0,
    )
    fat = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=0.65, n=300, lo=0.6, hi=0.7),
        spot=100.0,
    )
    assert thin.uncertainty > fat.uncertainty


def test_uncertainty_in_zero_one_range():
    r = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=0.7, n=5, lo=0.1, hi=0.95),
        spot=100.0,
    )
    assert 0.0 <= r.uncertainty <= 1.0


def test_ci_width_explicit_field_overrides_bounds():
    r = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=0.65, n=200, ci_width=0.5),
        spot=100.0,
    )
    # 0.5 × 1.5 + (1 - 1.0) = 0.75
    assert r.uncertainty == pytest.approx(0.75, abs=1e-4)


def test_rationale_tags_strong_and_high_sample():
    r = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=0.7, n=300, lo=0.65, hi=0.75),
        spot=100.0,
    )
    assert "strong_posterior" in r.rationale_tags
    assert "high_sample" in r.rationale_tags


def test_rationale_tags_wide_ci_when_above_threshold():
    r = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=0.65, n=100, lo=0.40, hi=0.85),
        spot=100.0,
    )
    assert "wide_ci" in r.rationale_tags


def test_fast_compose_all_returns_dict_for_each_pattern():
    knowledge = {
        "bull_flag": _cohort(post=0.65, n=200, lo=0.6, hi=0.7),
        "bear_flag": _cohort(post=0.62, n=120, lo=0.55, hi=0.69),
        "pennant":   _cohort(post=0.70, n=200, lo=0.65, hi=0.75),
    }
    results = fast_compose_all(
        ticker="NVDA", knowledge=knowledge, spot=100.0,
    )
    assert set(results.keys()) == set(knowledge.keys())
    for v in results.values():
        assert isinstance(v, FastComposerResult)


def test_to_dict_carries_all_fields():
    r = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=0.65, n=200, lo=0.6, hi=0.7),
        spot=100.0,
    )
    d = r.to_dict()
    for key in (
        "pattern", "action", "direction", "rank", "uncertainty",
        "headline", "thesis_paragraph", "suggested_action",
        "invalidation", "rationale_tags",
    ):
        assert key in d, f"missing key {key}"


def test_headline_contains_posterior_and_sample_size():
    r = fast_compose_one(
        ticker="NVDA", pattern="bull_flag",
        cohort=_cohort(post=0.68, n=147),
        spot=100.0,
    )
    assert "68%" in r.headline
    assert "147" in r.headline


def test_skipped_pattern_has_no_suggested_action_dict():
    r = fast_compose_one(
        ticker="NVDA", pattern="pennant",
        cohort=_cohort(post=0.9, n=500),
        spot=100.0,
    )
    assert r.action == "SKIP"
    assert r.suggested_action is None
