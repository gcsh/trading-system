"""MITS Phase 14.C — Simulator council agent vote shape.

High ``p_max_loss`` → council vote is ABSTAIN with reasoning starting
``simulator_veto:`` AND ``context["simulator_verdict"]["reject_reason"]``
is populated. Normal verdicts produce a directional vote with
``confidence == verdict.conviction_score``.
"""
from __future__ import annotations

import pytest

from backend.bot.agents import STANCE_ABSTAIN, STANCE_BUY, STANCE_SELL
from backend.bot.agents.simulator_agent import agent_simulator
from backend.bot.analysis.simulator import reset_cache


@pytest.fixture(autouse=True)
def _wipe(monkeypatch):
    import backend.bot.ai.vector_store as vs
    monkeypatch.setattr(vs, "embed", lambda text: [])
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: [])
    reset_cache()
    yield
    reset_cache()


def _ctx(*, action="BUY", cells=None):
    return {
        "ticker": "AAPL",
        "action": action,
        "snapshot": {"price": 200.0},
        "analytics": {"regime": {"trend": "bullish",
                                  "volatility": "normal"}},
        "knowledge_evidence": {"cells": cells or []},
    }


def test_high_max_loss_triggers_veto_vote():
    """Single deeply-negative cohort cell — every projected payoff is
    ≤ max_loss for a long stock (50% adverse move), so p_max_loss = 100%
    and the veto fires."""
    cells = [{"sample_size": 100, "avg_return_pct": -0.60}]
    ctx = _ctx(action="BUY", cells=cells)
    vote = agent_simulator(ctx)
    assert vote.stance == STANCE_ABSTAIN
    # Reasoning begins with the veto tag the engine looks for.
    assert vote.reasoning.startswith("simulator_veto:")
    # Verdict is published on the context for the engine to consume.
    sv = ctx["simulator_verdict"]
    assert sv["reject_reason"] is not None
    assert sv["reject_reason"].startswith("simulator_veto:")
    assert sv["p_max_loss"] > 0.30


def test_normal_cohort_produces_directional_vote():
    cells = [{"sample_size": 200, "avg_return_pct": 0.015}]
    ctx = _ctx(action="BUY", cells=cells)
    vote = agent_simulator(ctx)
    assert vote.stance == STANCE_BUY
    sv = ctx["simulator_verdict"]
    assert sv["reject_reason"] is None
    # Council confidence == verdict conviction_score (bit identical).
    assert vote.confidence == sv["conviction_score"]


def test_short_action_yields_sell_stance():
    cells = [{"sample_size": 200, "avg_return_pct": -0.015}]
    ctx = _ctx(action="SELL", cells=cells)
    vote = agent_simulator(ctx)
    assert vote.stance == STANCE_SELL


def test_empty_cohort_still_emits_directional_vote_from_mc_iv_fallback():
    """Empty cohort + no pgvector hits → the Monte Carlo path drops
    onto the IV-regime fallback for sigma and still produces a verdict
    (sample_size = n_paths). The council vote is directional, not silent."""
    ctx = _ctx(action="BUY", cells=[])
    vote = agent_simulator(ctx)
    # Direction-aware vote (BUY on a long action).
    assert vote.stance == STANCE_BUY
    sv = ctx["simulator_verdict"]
    # MC always runs n_paths > 0; verdict is well-formed.
    assert sv["sample_size"] > 0
    assert sv["reject_reason"] is None


def test_missing_ticker_silent_abstain():
    ctx = _ctx(action="BUY")
    ctx["ticker"] = ""
    vote = agent_simulator(ctx)
    assert vote.stance == STANCE_ABSTAIN
    assert "ticker" in vote.reasoning or "spot" in vote.reasoning
