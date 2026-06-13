"""MITS Phase 14.C — process-local verdict cache (Gate A).

Second call within the same five-minute bucket returns ``cache_hit=True``
with bit-identical fields. Different inputs miss.
"""
from __future__ import annotations

import pytest

from backend.bot.analysis.simulator import SimulatorAgent, reset_cache


@pytest.fixture(autouse=True)
def _wipe(monkeypatch):
    """Stub pgvector so the analog path is hermetic + reset cache around
    each test."""
    import backend.bot.ai.vector_store as vs
    monkeypatch.setattr(vs, "embed", lambda text: [])
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: [])
    reset_cache()
    yield
    reset_cache()


def test_cache_hit_returns_identical_verdict_within_bucket():
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
    # Every numeric field must be bit-identical.
    assert v1.expected_payoff == v2.expected_payoff
    assert v1.p_win == v2.p_win
    assert v1.p_max_loss == v2.p_max_loss
    assert v1.payoff_std == v2.payoff_std
    assert v1.max_drawdown_pctile_5 == v2.max_drawdown_pctile_5
    assert v1.conviction_score == v2.conviction_score
    assert v1.sample_size == v2.sample_size
    assert v1.mode == v2.mode
    assert v1.reject_reason == v2.reject_reason


def test_different_direction_misses_cache():
    sim = SimulatorAgent()
    cells = [{"sample_size": 100, "avg_return_pct": 0.02}]
    base = dict(
        ticker="AAPL", pattern="x", regime="bullish",
        vol_state="normal", spot=100.0, strike=None, dte=5,
        cohort_cells=cells, n_paths=1_000, analog_k=20,
    )
    v_long = sim.simulate(direction="long_stock", **base)
    v_short = sim.simulate(direction="short_stock", **base)
    assert v_long.cache_hit is False
    assert v_short.cache_hit is False
    # Long and short on the same cohort should land on different numbers.
    assert v_long.expected_payoff != v_short.expected_payoff


def test_different_ticker_misses_cache():
    sim = SimulatorAgent()
    cells = [{"sample_size": 100, "avg_return_pct": 0.02}]
    base = dict(
        pattern="x", regime="bullish", vol_state="normal",
        direction="long_stock", spot=100.0, strike=None, dte=5,
        cohort_cells=cells, n_paths=500, analog_k=20,
    )
    v_aapl = sim.simulate(ticker="AAPL", **base)
    v_msft = sim.simulate(ticker="MSFT", **base)
    assert v_aapl.cache_hit is False
    assert v_msft.cache_hit is False
