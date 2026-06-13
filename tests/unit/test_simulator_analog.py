"""MITS Phase 14.C — analog roll-forward + cohort-cell fallback.

The pgvector layer + outcome tables are stubbed via monkeypatch so the
test is hermetic. We assert payoff projection math is right and that
the cohort-cell fallback fires when no pgvector hits land.
"""
from __future__ import annotations

import pytest

from backend.bot.analysis.simulator import (
    SimulatorAgent, MODE_ANALOG, reset_cache,
)


@pytest.fixture(autouse=True)
def _wipe_cache():
    reset_cache()
    yield
    reset_cache()


def test_analog_falls_back_to_cohort_cells_when_pgvector_empty(monkeypatch):
    """When pgvector returns no hits, the analog path synthesizes
    samples from the cohort cells. Long stock with +2% avg cohort
    return at $100 spot → ~$2/share expected payoff."""
    import backend.bot.ai.vector_store as vs

    monkeypatch.setattr(vs, "embed", lambda text: [])
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: [])

    sim = SimulatorAgent()
    cells = [
        {"sample_size": 100, "avg_return_pct": 0.02},  # +2% cohort
        {"sample_size": 50, "avg_return_pct": 0.025},
    ]
    verdict = sim._analog_rollforward(
        ticker="AAPL", pattern="rsi_oversold", regime="bullish",
        vol_state="normal", direction="long_stock", spot=100.0,
        strike=None, dte=5, cohort_cells=cells, analog_k=50,
    )
    assert verdict.mode == MODE_ANALOG
    assert verdict.sample_size > 0
    # Empirical mean payoff per share ≈ cohort avg * spot.
    assert 1.5 <= verdict.expected_payoff <= 3.0
    # All synthesized samples are positive → p_win == 1.
    assert verdict.p_win >= 0.9


def test_analog_returns_zero_sample_when_no_cells_and_no_hits(monkeypatch):
    import backend.bot.ai.vector_store as vs
    monkeypatch.setattr(vs, "embed", lambda text: [])
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: [])
    sim = SimulatorAgent()
    verdict = sim._analog_rollforward(
        ticker="AAPL", pattern="x", regime="r", vol_state="v",
        direction="long_stock", spot=100.0, strike=None, dte=5,
        cohort_cells=[], analog_k=50,
    )
    assert verdict.sample_size == 0
    assert verdict.expected_payoff == 0.0


def test_analog_short_stock_inverts_payoff(monkeypatch):
    import backend.bot.ai.vector_store as vs
    monkeypatch.setattr(vs, "embed", lambda text: [])
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: [])
    sim = SimulatorAgent()
    cells = [{"sample_size": 100, "avg_return_pct": 0.03}]   # +3% cohort
    verdict = sim._analog_rollforward(
        ticker="AAPL", pattern="x", regime="r", vol_state="v",
        direction="short_stock", spot=100.0, strike=None, dte=5,
        cohort_cells=cells, analog_k=50,
    )
    # Short on a positive-return cohort = negative payoff.
    assert verdict.expected_payoff < 0
    assert verdict.p_win < 0.5


def test_dte_to_horizon_buckets():
    assert SimulatorAgent._dte_to_horizon(None) == "1d"
    assert SimulatorAgent._dte_to_horizon(0) == "1d"
    assert SimulatorAgent._dte_to_horizon(1) == "1d"
    assert SimulatorAgent._dte_to_horizon(3) == "5d"
    assert SimulatorAgent._dte_to_horizon(5) == "5d"
    assert SimulatorAgent._dte_to_horizon(10) == "20d"
    assert SimulatorAgent._dte_to_horizon(30) == "20d"


def test_simulate_full_pipeline_returns_ensemble(monkeypatch):
    """When both analog (via cohort fallback) and MC succeed, the
    public ``simulate`` returns an ensembled verdict."""
    import backend.bot.ai.vector_store as vs
    monkeypatch.setattr(vs, "embed", lambda text: [])
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: [])
    sim = SimulatorAgent()
    cells = [
        {"sample_size": 100, "avg_return_pct": 0.015},
        {"sample_size": 80, "avg_return_pct": 0.02},
    ]
    verdict = sim.simulate(
        ticker="AAPL", pattern="rsi_oversold", regime="bullish",
        vol_state="normal", direction="long_stock", spot=200.0,
        strike=None, dte=5, cohort_cells=cells, n_paths=1_000,
        analog_k=30,
    )
    assert verdict.mode == "ensemble"
    assert verdict.sample_size > 1_000
    assert 0.0 <= verdict.p_win <= 1.0
    assert verdict.conviction_score >= 0.0
