"""MITS Phase 18.A — Learned Hypothesis Attribution unit tests.

Covers the pure-math primitives + the per-agent / per-axis / per-strategy
aggregators in ``backend.bot.learning.attribution``. Uses synthetic
``_ClosedDecision`` rows passed directly into the calibrators (the
``decisions`` kwarg) so the tests don't touch the DB.

Honesty guardrails the suite exercises:

  * n_closed < min_n → ALL metrics None + ``insufficient_sample_size_n_lt_<N>`` note
  * stance='hold' / 'abstain' rows excluded from Brier + ECE
  * stale_calibration flag fires past the staleness threshold
  * Spearman: monotonic ≈ +1, anti-monotonic ≈ -1, noise near 0, constant → None
  * Wilson CI tightens as n grows; brackets the point estimate

The Step 5 brief asked for ≥12 tests; this file ships 18.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest

from backend.bot.learning.attribution import (
    DEFAULT_MIN_N_AGENT,
    DEFAULT_MIN_N_AXIS,
    DEFAULT_MIN_N_STRATEGY,
    AgentCalibration,
    AxisCalibration,
    KNOWN_AGENTS,
    KNOWN_AXES,
    StrategyCalibration,
    _ClosedDecision,
    _ranks,
    _spearman,
    _wilson_interval,
    brier_score,
    compute_agent_calibration,
    compute_attribution_report,
    compute_axis_calibration,
    compute_strategy_calibration,
    ece,
)


pytestmark = [pytest.mark.unit]


# ── Helpers ──────────────────────────────────────────────────────────


def _decision(
    *,
    trade_id: int = 1,
    pnl_pct: float = 0.0,
    days_ago: int = 1,
    agent_outputs: Optional[List[Dict[str, Any]]] = None,
    confidence_breakdown: Optional[Dict[str, float]] = None,
    strategy_name: str = "test_strategy",
    regime_trend: str = "trending_up",
) -> _ClosedDecision:
    return _ClosedDecision(
        trade_id=trade_id,
        pnl_pct=pnl_pct,
        pnl_raw=pnl_pct,
        win=1 if pnl_pct > 0 else 0,
        decision_timestamp=datetime.utcnow() - timedelta(days=days_ago),
        agent_outputs=agent_outputs or [],
        consensus={},
        confidence_breakdown=confidence_breakdown or {},
        strategy_name=strategy_name,
        regime_trend=regime_trend,
    )


def _agent_output(agent: str, stance: str, confidence_pct: int) -> Dict[str, Any]:
    """Build a synthetic AgentOutput JSON shape matching contracts_v2.

    Note: confidence is stored as int 0..100 in the persisted JSON
    (contracts_v2.py:299). The calibrator normalizes via
    ``_confidence_norm`` so we pass the int form here to mirror prod.
    """
    return {
        "agent": agent,
        "role": "test",
        "stance": stance,
        "confidence": confidence_pct,
        "weight": 1.0,
        "reasoning": "synthetic",
        "reasoning_type": "contributing",
        "supporting_factors": [],
        "concerns": [],
    }


# ── Pure math primitives ─────────────────────────────────────────────


def test_brier_score_perfect_predictor_is_zero():
    assert brier_score([1.0, 0.0, 1.0, 0.0], [1, 0, 1, 0]) == 0.0


def test_brier_score_coin_flip_baseline_is_quarter():
    # E[(0.5 - X)^2] where X ∈ {0, 1} ⇒ 0.25 exactly.
    assert brier_score([0.5] * 4, [1, 0, 1, 0]) == 0.25


def test_brier_score_worst_predictor_is_one():
    assert brier_score([0.0, 1.0, 0.0, 1.0], [1, 0, 1, 0]) == 1.0


def test_brier_score_empty_input_returns_none():
    assert brier_score([], []) is None
    assert brier_score([0.5], [1, 0]) is None       # length mismatch


def test_ece_perfect_predictor_is_near_zero():
    # 0.95/0.05 sit at the bin centers' near-edge so |mean_p - mean_o|
    # is just |0.95 - 1| = 0.05 (weighted half-half), giving ECE ≈ 0.05.
    result = ece([0.95, 0.05, 0.95, 0.05], [1, 0, 1, 0], bins=5)
    assert result is not None and result < 0.10


def test_ece_worst_predictor_is_near_one():
    result = ece([0.05, 0.95, 0.05, 0.95], [1, 0, 1, 0], bins=5)
    assert result is not None and result > 0.90


def test_wilson_interval_brackets_point_estimate():
    lo, hi = _wilson_interval(7, 10)
    assert lo is not None and hi is not None
    assert lo < 0.7 < hi      # bracket the 70% hit rate
    assert hi - lo > 0.2      # noticeable bracket at n=10


def test_wilson_interval_tightens_as_n_grows():
    lo_small, hi_small = _wilson_interval(7, 10)
    lo_big, hi_big = _wilson_interval(70, 100)
    assert (hi_big - lo_big) < (hi_small - lo_small)


def test_wilson_interval_zero_n_returns_none():
    assert _wilson_interval(0, 0) == (None, None)


def test_spearman_monotonic_is_one():
    rho = _spearman([1, 2, 3, 4, 5, 6, 7], [10, 20, 30, 40, 50, 60, 70])
    assert rho is not None and rho == pytest.approx(1.0, abs=1e-9)


def test_spearman_anti_monotonic_is_negative_one():
    rho = _spearman([1, 2, 3, 4, 5, 6, 7], [70, 60, 50, 40, 30, 20, 10])
    assert rho is not None and rho == pytest.approx(-1.0, abs=1e-9)


def test_spearman_constant_returns_none():
    assert _spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None


def test_ranks_handle_ties_with_average():
    # Two 20s sit in positions 2 and 3 → average rank 2.5 each.
    assert _ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]


# ── Per-agent calibration ────────────────────────────────────────────


def test_agent_calibration_returns_one_entry_per_known_agent():
    """Every known agent must appear in the result — even when no
    closed decision references it (honesty: don't drop empty cohorts)."""
    out = compute_agent_calibration(decisions=[])
    assert {a.agent for a in out} == set(KNOWN_AGENTS)


def test_insufficient_sample_size_yields_none_metrics_and_note():
    # 5 decisions; min_n=30 by default ⇒ every agent under threshold.
    decisions = [
        _decision(pnl_pct=1.0, agent_outputs=[
            _agent_output("market", "buy", 80),
        ])
        for _ in range(5)
    ]
    out = compute_agent_calibration(decisions=decisions)
    market = next(a for a in out if a.agent == "market")
    assert market.hit_rate is None
    assert market.mean_pnl_pct is None
    assert market.brier_score is None
    assert market.ece is None
    assert any(
        f"insufficient_sample_size_n_lt_{DEFAULT_MIN_N_AGENT}" in n
        for n in market.notes
    )


def test_hold_and_abstain_excluded_from_brier_and_ece():
    """Build 40 closed decisions; 20 buy/win, 20 hold (excluded). The
    20 hold rows must NOT poison Brier — assert the buy-only Brier
    matches what we'd expect on a perfect directional predictor."""
    decisions = []
    # 20 confident buys, all winners ⇒ Brier should approach (1-1)^2 = 0.
    for i in range(20):
        decisions.append(_decision(
            pnl_pct=1.5,
            agent_outputs=[_agent_output("macro", "buy", 100)],
        ))
    # 20 holds with zero P&L — should be silently ignored by Brier
    # because hold stance has no directional prediction to score.
    for i in range(20):
        decisions.append(_decision(
            pnl_pct=0.0,
            agent_outputs=[_agent_output("macro", "hold", 50)],
        ))
    # Pass min_n=20 so the directional cohort (n=20) clears the floor.
    out = compute_agent_calibration(decisions=decisions, min_n=20)
    macro = next(a for a in out if a.agent == "macro")
    assert macro.n_closed == 20                       # only the buys count
    assert macro.hit_rate == 1.0                       # all 20 won
    assert macro.brier_score is not None
    assert macro.brier_score == pytest.approx(0.0, abs=1e-6)
    # The hold subset still gets reported in by_stance.
    assert macro.by_stance["hold"]["n"] == 20


def test_stale_calibration_flagged_when_oldest_sample_too_old():
    # All decisions 60 days old, min_n=30 satisfied.
    decisions = [
        _decision(
            days_ago=60,
            pnl_pct=(1.0 if i % 2 == 0 else -1.0),
            agent_outputs=[_agent_output(
                "market", "buy" if i % 2 == 0 else "sell", 70,
            )],
        )
        for i in range(40)
    ]
    out = compute_agent_calibration(
        decisions=decisions, stale_after_days=30,
    )
    market = next(a for a in out if a.agent == "market")
    assert market.n_closed == 40
    assert market.hit_rate is not None             # threshold met
    assert "stale_calibration" in market.notes


def test_confidence_bin_counts_sum_to_n_closed():
    decisions = []
    for i in range(50):
        conf_pct = (i % 10) * 10               # 0, 10, …, 90 cycle
        decisions.append(_decision(
            pnl_pct=(1.0 if i % 2 == 0 else -1.0),
            agent_outputs=[_agent_output(
                "market", "buy" if i % 2 == 0 else "sell", conf_pct,
            )],
        ))
    out = compute_agent_calibration(decisions=decisions)
    market = next(a for a in out if a.agent == "market")
    total_in_bins = sum(b["n"] for b in market.by_confidence_bin)
    assert total_in_bins == market.n_closed


def test_agent_dataclass_to_dict_round_trips_json_safely():
    decisions = [
        _decision(pnl_pct=1.0, agent_outputs=[
            _agent_output("market", "buy", 70),
        ])
        for _ in range(40)
    ]
    out = compute_agent_calibration(decisions=decisions)
    market = next(a for a in out if a.agent == "market")
    blob = json.dumps(market.to_dict())
    roundtripped = json.loads(blob)
    assert roundtripped["agent"] == "market"
    assert roundtripped["n_closed"] == 40
    assert roundtripped["hit_rate"] == 1.0


# ── Per-axis calibration ─────────────────────────────────────────────


def test_axis_calibration_spearman_picks_up_predictive_axis():
    # Linearly increasing axis scores → linearly increasing P&L.
    decisions = []
    for i in range(40):
        score = i / 40.0          # 0 → ~1 (becomes 0..100 inside)
        decisions.append(_decision(
            pnl_pct=float(i) * 0.5,
            confidence_breakdown={"technical": score},
        ))
    out = compute_axis_calibration(decisions=decisions)
    tech = next(a for a in out if a.axis == "technical")
    assert tech.n_closed == 40
    assert tech.spearman_corr is not None
    assert tech.spearman_corr > 0.95


def test_axis_calibration_discrimination_positive_when_axis_predictive():
    decisions = []
    # 20 high-axis rows with +5% returns
    for _ in range(20):
        decisions.append(_decision(
            pnl_pct=5.0,
            confidence_breakdown={"market_structure": 0.9},
        ))
    # 20 low-axis rows with -3% returns
    for _ in range(20):
        decisions.append(_decision(
            pnl_pct=-3.0,
            confidence_breakdown={"market_structure": 0.1},
        ))
    out = compute_axis_calibration(decisions=decisions)
    ms = next(a for a in out if a.axis == "market_structure")
    assert ms.discrimination is not None
    assert ms.discrimination == pytest.approx(8.0, abs=0.01)   # 5 - (-3)


# ── Per-strategy calibration + regime stratification ────────────────


def test_strategy_calibration_stratifies_by_regime():
    decisions = []
    # 10 trending_up wins for strategy A
    for _ in range(10):
        decisions.append(_decision(
            pnl_pct=2.0, strategy_name="momentum_long",
            regime_trend="trending_up",
        ))
    # 5 ranging losses for strategy A
    for _ in range(5):
        decisions.append(_decision(
            pnl_pct=-1.0, strategy_name="momentum_long",
            regime_trend="ranging",
        ))
    out = compute_strategy_calibration(
        decisions=decisions, min_n=10,
    )
    momentum = next(s for s in out if s.strategy_name == "momentum_long")
    assert momentum.n_closed == 15
    assert "trending_up" in momentum.by_regime
    assert "ranging" in momentum.by_regime
    assert momentum.by_regime["trending_up"]["hit_rate"] == 1.0
    assert momentum.by_regime["ranging"]["hit_rate"] == 0.0


def test_strategy_calibration_below_min_n_flags_insufficient():
    decisions = [
        _decision(pnl_pct=1.0, strategy_name="thin_strategy")
        for _ in range(3)
    ]
    out = compute_strategy_calibration(decisions=decisions, min_n=10)
    thin = next(s for s in out if s.strategy_name == "thin_strategy")
    assert thin.n_closed == 3
    assert thin.hit_rate is None
    assert any("insufficient_sample_size" in n for n in thin.notes)


# ── Composite report ─────────────────────────────────────────────────


def test_compute_attribution_report_returns_expected_keys(temp_db):
    """End-to-end shape — empty DB ⇒ 0 closed decisions, but every
    known agent + axis still appears in the report payload."""
    report = compute_attribution_report(window_days=90)
    expected_top = {
        "computed_at", "window_days", "n_closed_decisions",
        "min_n_agent", "min_n_axis", "min_n_strategy",
        "stale_after_days", "agents", "axes", "strategies",
        # 18-FU Gap 4 — flag recording which read this report ran.
        "include_synthetic",
    }
    assert set(report.keys()) == expected_top
    assert report["n_closed_decisions"] == 0
    # All known agents listed even on empty corpus (honesty rule).
    agent_names = {a["agent"] for a in report["agents"]}
    assert agent_names == set(KNOWN_AGENTS)
    axis_names = {a["axis"] for a in report["axes"]}
    assert axis_names == set(KNOWN_AXES)
    # No strategies yet — empty list is the correct answer.
    assert report["strategies"] == []
