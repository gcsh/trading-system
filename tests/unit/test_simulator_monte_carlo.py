"""MITS Phase 14.C — Monte Carlo path generator + verdict math.

Fixes the seed via ``TUNABLES.simulator_mc_seed`` so the same call
returns bit-identical numbers in repeat runs — the cache test (Gate A)
relies on this property too.
"""
from __future__ import annotations

import pytest

from backend.bot.analysis.simulator import (
    SimulatorAgent, SimulatorVerdict, MODE_MONTE_CARLO, reset_cache,
)


@pytest.fixture(autouse=True)
def _wipe_cache():
    reset_cache()
    yield
    reset_cache()


def test_monte_carlo_returns_verdict_with_documented_fields():
    sim = SimulatorAgent()
    verdict = sim._monte_carlo(
        ticker="AAPL", direction="long_stock", spot=200.0,
        strike=None, dte=20, cohort_cells=[], n_paths=2_000,
    )
    assert isinstance(verdict, SimulatorVerdict)
    # Documented fields surface on to_dict().
    d = verdict.to_dict()
    for key in ("mode", "expected_payoff", "p_win", "p_max_loss",
                "payoff_std", "max_drawdown_pctile_5",
                "conviction_score", "sample_size", "cache_hit",
                "reject_reason"):
        assert key in d, f"missing {key}"
    assert verdict.mode == MODE_MONTE_CARLO
    assert verdict.sample_size == 2_000
    assert 0.0 <= verdict.p_win <= 1.0
    assert 0.0 <= verdict.p_max_loss <= 1.0


def test_monte_carlo_is_reproducible_under_fixed_seed():
    """Two MC runs with the same inputs must produce identical numbers
    (cache disabled in the fixture, so this exercises the GBM RNG path,
    not the verdict cache)."""
    sim = SimulatorAgent()
    v1 = sim._monte_carlo(
        ticker="AAPL", direction="long_stock", spot=200.0,
        strike=None, dte=10, cohort_cells=[], n_paths=1_000,
    )
    v2 = sim._monte_carlo(
        ticker="AAPL", direction="long_stock", spot=200.0,
        strike=None, dte=10, cohort_cells=[], n_paths=1_000,
    )
    assert v1.expected_payoff == v2.expected_payoff
    assert v1.p_win == v2.p_win
    assert v1.payoff_std == v2.payoff_std


def test_monte_carlo_positive_drift_yields_positive_expected_payoff():
    """High-drift cohort should yield positive expected payoff for a
    long stock — sanity check on direction."""
    sim = SimulatorAgent()
    # 5% mean return over a 5-day cohort with low dispersion.
    cells = [
        {"sample_size": 100, "avg_return_pct": 0.05},
        {"sample_size": 100, "avg_return_pct": 0.045},
        {"sample_size": 100, "avg_return_pct": 0.055},
    ]
    verdict = sim._monte_carlo(
        ticker="AAPL", direction="long_stock", spot=100.0,
        strike=None, dte=5, cohort_cells=cells, n_paths=3_000,
    )
    assert verdict.expected_payoff > 0
    assert verdict.p_win > 0.5


def test_monte_carlo_handles_short_direction():
    sim = SimulatorAgent()
    # Multiple cells with consistently-negative cohort returns — sigma
    # is non-degenerate so the MC path uses cohort drift, not IV fallback.
    cells = [
        {"sample_size": 100, "avg_return_pct": -0.04},
        {"sample_size": 100, "avg_return_pct": -0.05},
        {"sample_size": 100, "avg_return_pct": -0.045},
    ]
    verdict_short = sim._monte_carlo(
        ticker="AAPL", direction="short_stock", spot=100.0,
        strike=None, dte=5, cohort_cells=cells, n_paths=3_000,
    )
    verdict_long = sim._monte_carlo(
        ticker="AAPL", direction="long_stock", spot=100.0,
        strike=None, dte=5, cohort_cells=cells, n_paths=3_000,
    )
    # Short payoff must be the additive inverse of long payoff per path,
    # so E[short] = -E[long] regardless of the underlying drift.
    assert verdict_short.expected_payoff == pytest.approx(
        -verdict_long.expected_payoff, abs=1e-4)
    # Long on a falling cohort has p_win < 0.5; short must invert.
    assert verdict_long.p_win < 0.5
    assert verdict_short.p_win > 0.5


def test_monte_carlo_long_call_payoff_uses_bs_pricing():
    sim = SimulatorAgent()
    # Tight bullish cohort.
    cells = [{"sample_size": 200, "avg_return_pct": 0.06}]
    verdict = sim._monte_carlo(
        ticker="AAPL", direction="long_call", spot=100.0,
        strike=100.0, dte=10, cohort_cells=cells, n_paths=2_000,
    )
    assert verdict.sample_size == 2_000
    # E[payoff] is per-contract (× 100); should be non-trivial on an ATM
    # call with bullish drift over 10 days.
    assert abs(verdict.expected_payoff) > 1.0
