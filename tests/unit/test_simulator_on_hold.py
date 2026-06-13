"""MITS Phase 19 — Simulator scenario decomposition on HOLD events.

Pins:
  * ``BotEngine._ensure_simulator_scenarios_on_hold`` re-runs the
    simulator when ``simulator_verdict.scenarios`` is empty and the
    ``scenario_decomposition_on_hold`` tunable is True.
  * Pseudo-analog fallback off ``knowledge_evidence`` cohort cells
    when pgvector is cold but cells carry ``avg_return_pct``.
  * Tunable=False short-circuits the helper without touching the
    simulator (zero compute regression when an operator wants to
    skip the extra work).
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from backend.bot.engine import BotEngine, _PseudoAnalog


pytestmark = [pytest.mark.unit]


def _engine() -> BotEngine:
    """Build a BotEngine that bypasses heavyweight constructor wiring.

    The HOLD-scenario helper is method-only — it doesn't touch broker /
    market-data / strategy state — so a bare instance works.
    """
    eng = BotEngine.__new__(BotEngine)
    return eng


def _event(
    *,
    ticker: str = "AAPL",
    simulator_verdict: Dict[str, Any] = None,
    cohort_cells: List[Dict[str, Any]] = None,
    spot: float = 195.0,
) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "action": "HOLD",
        "snapshot": {"price": spot},
        "analytics": {"regime": {"trend": "bullish", "volatility": "low"}},
        "knowledge_evidence": {"cells": cohort_cells or []},
        "simulator_verdict": simulator_verdict,
    }


def test_skip_when_tunable_disabled(monkeypatch):
    """Tunable OFF → helper returns immediately, never imports the
    simulator module — proves the gate is real."""
    from backend.config import TUNABLES
    monkeypatch.setattr(
        TUNABLES, "scenario_decomposition_on_hold", False, raising=False,
    )
    eng = _engine()
    ev = _event(simulator_verdict=None)
    # Trip a SimulatorAgent import sentry — should never fire.
    import backend.bot.analysis.simulator as sim_mod
    original = sim_mod.SimulatorAgent
    poisoned = MagicMock(side_effect=AssertionError(
        "simulator must not be invoked when tunable is OFF"))
    monkeypatch.setattr(sim_mod, "SimulatorAgent", poisoned)
    try:
        eng._ensure_simulator_scenarios_on_hold(ev)
    finally:
        monkeypatch.setattr(sim_mod, "SimulatorAgent", original)
    assert ev["simulator_verdict"] is None


def test_skip_when_scenarios_already_populated(monkeypatch):
    """Existing scenarios → no-op (preserves cache-hit semantics)."""
    from backend.config import TUNABLES
    monkeypatch.setattr(
        TUNABLES, "scenario_decomposition_on_hold", True, raising=False,
    )
    eng = _engine()
    existing = [{"label": "continuation", "probability": 0.4,
                 "expected_payoff": 1.0, "payoff_std": 0.2, "n_analogs": 8}]
    sv = {"mode": "analog", "scenarios": existing, "p_win": 0.6}
    ev = _event(simulator_verdict=sv)

    import backend.bot.analysis.simulator as sim_mod
    poisoned = MagicMock(side_effect=AssertionError(
        "simulator must not be invoked when scenarios are already set"))
    monkeypatch.setattr(sim_mod, "SimulatorAgent", poisoned)
    eng._ensure_simulator_scenarios_on_hold(ev)
    # Untouched.
    assert ev["simulator_verdict"]["scenarios"] == existing


def test_cohort_cell_pseudo_analog_fallback(monkeypatch):
    """pgvector empty + cohort cells with returns → synthesize pseudo
    analogs and decompose into scenario clusters."""
    from backend.config import TUNABLES
    monkeypatch.setattr(
        TUNABLES, "scenario_decomposition_on_hold", True, raising=False,
    )
    # Stub SimulatorAgent so its analog pull comes back empty (forcing
    # the cohort-cell fallback branch).
    import backend.bot.analysis.simulator as sim_mod

    class _StubVerdict:
        def to_dict(self):
            return {"mode": "monte_carlo", "scenarios": [],
                    "p_win": 0.5, "expected_payoff": 0.0}

    class _StubAgent:
        def simulate(self, **kwargs):
            return _StubVerdict()

    monkeypatch.setattr(sim_mod, "SimulatorAgent", _StubAgent)
    eng = _engine()
    cells = [
        # continuation bucket (decimal 0.10 → +10%)
        {"avg_return_pct": 0.10, "sample_size": 20, "pattern": "p"},
        # fake_breakout bucket (decimal 0.01 → +1%)
        {"avg_return_pct": 0.01, "sample_size": 30, "pattern": "p"},
        # stop_out bucket (decimal -0.05 → -5%)
        {"avg_return_pct": -0.05, "sample_size": 10, "pattern": "p"},
    ]
    ev = _event(simulator_verdict=None, cohort_cells=cells)
    eng._ensure_simulator_scenarios_on_hold(ev)

    sv = ev["simulator_verdict"]
    assert isinstance(sv, dict)
    scenarios = sv.get("scenarios") or []
    assert scenarios, "expected non-empty scenarios from cohort fallback"
    labels = {s["label"] for s in scenarios}
    # Each cohort cell's mean lives in a different bucket.
    assert "continuation" in labels
    assert "fake_breakout" in labels
    assert "stop_out" in labels
    # Probabilities are non-negative and sum to ~1.0.
    total = sum(s["probability"] for s in scenarios)
    assert 0.99 <= total <= 1.01


def test_pseudo_analog_dataclass_minimum_shape():
    """_PseudoAnalog is the duck-type passed into decompose_scenarios;
    only ``realized_return_pct`` is read off it."""
    pa = _PseudoAnalog(realized_return_pct=12.5)
    assert pa.realized_return_pct == 12.5
    # decompose_scenarios reads the attribute by name (not positional).
    from backend.bot.analysis.simulator import decompose_scenarios
    clusters = decompose_scenarios(
        [pa, _PseudoAnalog(-15.0), _PseudoAnalog(1.0)],
        direction="long_stock", spot=100.0,
    )
    labels = {c.label for c in clusters}
    assert "continuation" in labels
    assert "macro_shock" in labels
    assert "fake_breakout" in labels
