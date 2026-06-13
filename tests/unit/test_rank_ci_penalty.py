"""MITS Phase 14.A — wide CI shrinks rank composite by expected coefficient."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.bot.ranker import rank_trade
from backend.config import TUNABLES


pytestmark = [pytest.mark.unit]


def _signal_inputs(direction="LONG", probability=0.7, rr=2.0):
    probability_obj = SimpleNamespace(
        direction=direction, probability=probability,
        risk_reward=rr, confidence=0.6,
    )
    regime_obj = SimpleNamespace(trend="bullish", volatility="normal")
    confluence_obj = SimpleNamespace(
        direction="bullish", score=0.6, conflicting_timeframes=[],
    )
    features = {
        "flow_bullishness": 0.5,
        "volume_ratio": 1.2,
        "pinning_probability": 0.0,
        "dominant_wall": "neutral",
    }
    return probability_obj, regime_obj, confluence_obj, features


def test_no_penalty_when_ci_width_absent():
    p, r, c, f = _signal_inputs()
    base = rank_trade(p, r, c, dict(f))
    assert "ci_penalty" not in base.components


def test_wide_ci_applies_multiplicative_penalty():
    p, r, c, f = _signal_inputs()
    base = rank_trade(p, r, c, dict(f))
    f["cohort_ci_width"] = 0.30
    penalised = rank_trade(p, r, c, f)
    assert "ci_penalty" in penalised.components
    expected = max(0.6, 1.0 - 0.30 * TUNABLES.rank_ci_penalty_coef)
    assert penalised.components["ci_penalty"] == pytest.approx(expected, abs=1e-3)
    assert penalised.score < base.score


def test_ci_penalty_clamped_at_floor():
    p, r, c, f = _signal_inputs()
    f["cohort_ci_width"] = 0.95  # huge — would push penalty below 0.6
    penalised = rank_trade(p, r, c, f)
    assert penalised.components["ci_penalty"] == pytest.approx(0.6, abs=1e-3)


def test_ci_penalty_reasoning_emitted_when_below_threshold():
    p, r, c, f = _signal_inputs()
    f["cohort_ci_width"] = 0.20  # 1 - 0.30 = 0.70 ← below 0.9 → reasoning
    penalised = rank_trade(p, r, c, f)
    has_reason = any("wide cohort CI" in line for line in penalised.reasoning)
    assert has_reason


def test_narrow_ci_does_not_emit_reasoning():
    p, r, c, f = _signal_inputs()
    f["cohort_ci_width"] = 0.04  # tiny → penalty 0.94 → no reasoning
    penalised = rank_trade(p, r, c, f)
    assert "ci_penalty" in penalised.components
    has_reason = any("wide cohort CI" in line for line in penalised.reasoning)
    assert has_reason is False


def test_grade_can_drop_after_ci_penalty():
    """A score just over a grade cutoff with no CI penalty can drop
    a grade when a wide CI is applied."""
    p, r, c, f = _signal_inputs(probability=0.78, rr=3.0)
    base = rank_trade(p, r, c, dict(f))
    f["cohort_ci_width"] = 0.40
    penalised = rank_trade(p, r, c, f)
    assert penalised.score <= base.score


def test_grade_explainer_field_present():
    """The dataclass field exists and is populated with a plain-English
    paragraph by 14.E."""
    p, r, c, f = _signal_inputs()
    out = rank_trade(p, r, c, f)
    assert hasattr(out, "grade_explainer")
    assert isinstance(out.grade_explainer, str)
    assert out.grade_explainer.startswith(f"This is a {out.grade}")
    assert "grade_explainer" in out.to_dict()
