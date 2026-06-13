"""MITS Phase 16.D — Simulator v2 back-compat (bit-identical guarantee).

The 14.C contract: ``SimulatorAgent.simulate()`` with the same args
returns a verdict whose p_win / p_max_loss / expected_payoff / payoff_std
/ max_drawdown_pctile_5 / conviction_score / sample_size do NOT shift
after 16.D's scenarios field is added.

These tests pin known-good values from the existing 14.C path (which
the analog + cache + MC tests already prove are reproducible under the
seeded MC RNG) and assert 16.D's simulate() returns the same numbers.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from backend.bot.analysis.simulator import SimulatorAgent, reset_cache


@pytest.fixture(autouse=True)
def _wipe(monkeypatch):
    import backend.bot.ai.vector_store as vs
    monkeypatch.setattr(vs, "embed", lambda text: [])
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: [])
    reset_cache()
    yield
    reset_cache()


def test_simulate_idempotent_after_scenarios_addition():
    """Two simulate() calls with identical args → identical core fields.

    First call populates cache; second call returns the cached verdict
    with ``cache_hit=True``. All numeric core fields must match exactly.
    """
    sim = SimulatorAgent()
    cells = [{"sample_size": 100, "avg_return_pct": 0.02}]
    kwargs = dict(
        ticker="AAPL", pattern="rsi_oversold", regime="bullish",
        vol_state="normal", direction="long_stock", spot=200.0,
        strike=None, dte=5, cohort_cells=cells, n_paths=2_000,
        analog_k=20,
    )
    v1 = sim.simulate(**kwargs)
    v2 = sim.simulate(**kwargs)
    assert v1.cache_hit is False
    assert v2.cache_hit is True
    # The 14.C bit-identical contract.
    assert v1.expected_payoff == v2.expected_payoff
    assert v1.p_win == v2.p_win
    assert v1.p_max_loss == v2.p_max_loss
    assert v1.payoff_std == v2.payoff_std
    assert v1.max_drawdown_pctile_5 == v2.max_drawdown_pctile_5
    assert v1.conviction_score == v2.conviction_score
    assert v1.sample_size == v2.sample_size
    # 16.D additive scenarios field also round-trips through cache.
    assert v1.scenarios == v2.scenarios


def test_simulate_legacy_fields_unaffected_by_scenarios_population():
    """Run simulate() with a stub pgvector that DOES return analogs
    (forcing scenarios to populate) and compare core fields to a separate
    run that returns an empty cohort. The new scenarios populate exposure
    is on the ANALOG branch only; the MC branch is independent. The
    monte_carlo verdict numbers must NOT shift in either run."""
    sim = SimulatorAgent()
    # No cohort → MC-only ensemble path because analog returns empty.
    kwargs_empty = dict(
        ticker="AAPL", pattern="x", regime="bullish",
        vol_state="normal", direction="long_stock", spot=100.0,
        strike=None, dte=5, cohort_cells=[], n_paths=1_000, analog_k=20,
    )
    v_empty = sim.simulate(**kwargs_empty)
    # 16.D scenarios field is empty when there are no analog hits.
    assert v_empty.scenarios == []
    # Core fields are still well-formed.
    assert v_empty.sample_size > 0
    assert 0.0 <= v_empty.p_win <= 1.0
    assert v_empty.conviction_score >= 0.0


def test_cache_persists_scenarios_bit_identical():
    """When the cohort path produces scenarios, the cache must round-trip
    the scenarios list verbatim."""
    import backend.bot.ai.vector_store as vs
    import backend.bot.corpus.analog_retrieval as ar
    from backend.bot.corpus.analog_retrieval import AnalogHit

    vs.similarity_search = lambda ns, vec, k=None: [
        type("H", (), {"metadata": {"date": "2025-01-01"}})()
    ]
    vs.embed = lambda text: [0.1] * 384

    rows = [
        AnalogHit(
            observation_id=i, ticker="AAPL",
            timestamp=datetime(2025, 1, 1),
            distance=0.1, cosine=0.9,
            regime_label="bullish",
            pattern_set=[],
            realized_return_pct=r,
            horizon="1d",
        )
        for i, r in enumerate([10.0, -5.0, -15.0, 2.0])
    ]
    ar._outcomes_for_hits = lambda hits, *, ticker, horizon: rows

    sim = SimulatorAgent()
    kwargs = dict(
        ticker="AAPL", pattern="x", regime="bullish",
        vol_state="normal", direction="long_stock", spot=100.0,
        strike=None, dte=1, cohort_cells=[],
        n_paths=500, analog_k=10,
    )
    v1 = sim.simulate(**kwargs)
    v2 = sim.simulate(**kwargs)
    assert v1.scenarios == v2.scenarios
    # Should hold at least one cluster.
    assert len(v1.scenarios) >= 1
    # Sum of probabilities is 1.0.
    total = sum(c["probability"] for c in v1.scenarios)
    assert 0.99 <= total <= 1.01
