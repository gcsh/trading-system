"""MITS Phase 16.B — AgentInput / AgentOutput contracts.

Pins:
  • AgentInput.legacy_context() round-trips the keys today's agents read
  • make_agent_input() builds an AgentInput from the loose context dict
    without losing any field
  • agent_output_from_vote() partitions key_drivers into
    supporting_factors / concerns based on KeyDriver.direction vs the
    handed-in consensus_direction
  • Confidence is rendered as integer percent (0-100)
  • Invalidation is the first invalidator string when present
"""
from __future__ import annotations

from backend.bot.agents import (
    AgentVote,
    STANCE_BUY,
    STANCE_SELL,
)
from backend.bot.agents.contract import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    KeyDriver,
    REASONING_CONTRIBUTING,
    REASONING_DISSENTING,
    RISK_MEDIUM,
)
from backend.bot.agents.contracts_v2 import (
    AgentInput,
    AgentOutput,
    agent_output_from_vote,
    make_agent_input,
)


def _drv(desc, cat="credit", direction=DIRECTION_LONG):
    return KeyDriver(
        description=desc, source_category=cat, direction=direction,
        weight=0.7, time_sensitive=False,
    )


def test_make_agent_input_carries_every_documented_field():
    ctx = {
        "ticker": "AAPL",
        "action": "BUY_CALL",
        "strategy": "trend_pullback",
        "snapshot": {"price": 100.0, "rsi": 60.0},
        "analytics": {
            "regime": {"trend": "bullish"},
            "features": {"vix": 14},
        },
        "macro": {"nfci": 0.1},
        "breadth": {"verdict": "bullish"},
        "cot_snapshot": {"net": 0.3},
        "earnings_intel": {"surprise_pct": 0.05},
        "insider_activity": {"recent_buys": 3},
        "short_pressure": {"days_to_cover": 2.1},
        "cross_asset": {"equities": "risk_on"},
        "portfolio_risk": {"net_beta": 0.5},
        "regime_vector": {"trend": {"value": "bullish"}, "iv_rank": {"value": "low"}},
        "strategy_matrix": {"top": "trend_call"},
        "knowledge_evidence": {"cells": [{"sample_size": 10}]},
    }
    ai = make_agent_input(ctx)
    assert isinstance(ai, AgentInput)
    assert ai.ticker == "AAPL"
    assert ai.action == "BUY_CALL"
    assert ai.proposed_direction == "long"
    assert ai.snapshot["price"] == 100.0
    # analytics.features flows into snapshot.features for lossless legacy
    assert ai.snapshot["features"]["vix"] == 14
    assert ai.regime_vector == ctx["regime_vector"]
    assert ai.strategy_matrix == ctx["strategy_matrix"]
    assert ai.historical_analogs == ctx["knowledge_evidence"]
    assert ai.risk_context["macro"] == ctx["macro"]
    assert ai.risk_context["breadth"] == ctx["breadth"]
    assert ai.risk_context["cot"] == ctx["cot_snapshot"]
    assert ai.risk_context["earnings_intel"] == ctx["earnings_intel"]
    assert ai.risk_context["insider"] == ctx["insider_activity"]
    assert ai.risk_context["short_pressure"] == ctx["short_pressure"]
    assert ai.portfolio_state["net_beta"] == 0.5


def test_agent_input_legacy_context_preserves_dict_consumers():
    ctx = {
        "ticker": "NVDA",
        "action": "BUY_PUT",
        "snapshot": {"price": 99.0},
        "analytics": {"features": {"iv_rank": 70}, "regime": {"trend": "bearish"}},
        "portfolio_risk": {"net_beta": -0.2},
        "regime_vector": {"trend": {"value": "bearish"}},
        "strategy_matrix": {"top": "put_spread"},
        "macro": {"nfci": 0.4},
    }
    ai = make_agent_input(ctx)
    legacy = ai.legacy_context()
    assert legacy["ticker"] == "NVDA"
    assert legacy["action"] == "BUY_PUT"
    assert legacy["snapshot"]["price"] == 99.0
    assert legacy["analytics"]["features"] == {"iv_rank": 70}
    assert legacy["analytics"]["regime"] == "bearish"
    assert legacy["portfolio_risk"] == {"net_beta": -0.2}
    assert legacy["regime_vector"] == ctx["regime_vector"]
    assert legacy["strategy_matrix"] == ctx["strategy_matrix"]
    assert legacy["macro"] == {"nfci": 0.4}


def test_agent_output_supporting_vs_concerns_for_long_consensus():
    vote = AgentVote(
        agent="market", role="Market Regime", stance=STANCE_BUY,
        confidence=0.75, weight=1.0, reasoning="bullish on breadth",
        reasoning_type=REASONING_CONTRIBUTING, risk_level=RISK_MEDIUM,
        invalidators=["SPY breaks 50dma"],
        key_drivers=[
            _drv("breadth 70%", cat="breadth", direction=DIRECTION_LONG),
            _drv("HY OAS rising", cat="credit", direction=DIRECTION_SHORT),
            _drv("flow bullish", cat="microstructure_flow", direction=DIRECTION_LONG),
        ],
    )
    out = agent_output_from_vote(vote, consensus_direction="long")
    assert isinstance(out, AgentOutput)
    assert out.agent == "market"
    assert out.stance == STANCE_BUY
    assert out.confidence == 75
    assert out.weight == 1.0
    assert out.invalidation == "SPY breaks 50dma"
    assert out.supporting_factors == ["breadth 70%", "flow bullish"]
    assert out.concerns == ["HY OAS rising"]
    assert set(out.source_categories) == {
        "breadth", "credit", "microstructure_flow",
    }


def test_agent_output_long_vote_against_short_consensus_inverts():
    """Same long-direction drivers, but consensus is short → drivers
    that voted long are now concerns; an opposing short driver becomes
    a supporter."""
    vote = AgentVote(
        agent="macro", role="Macro", stance=STANCE_SELL,
        confidence=0.40, weight=1.0, reasoning="mixed",
        reasoning_type=REASONING_DISSENTING, risk_level=RISK_MEDIUM,
        invalidators=[],
        key_drivers=[
            _drv("breadth thrust", cat="breadth", direction=DIRECTION_LONG),
            _drv("HY OAS blowout", cat="credit", direction=DIRECTION_SHORT),
        ],
    )
    out = agent_output_from_vote(vote, consensus_direction="short")
    assert out.supporting_factors == ["HY OAS blowout"]
    assert out.concerns == ["breadth thrust"]
    assert out.invalidation is None
    assert out.confidence == 40
    assert out.reasoning_type == REASONING_DISSENTING


def test_agent_output_round_trip_in_dict():
    vote = AgentVote(
        agent="x", role="X", stance=STANCE_BUY,
        confidence=0.5, weight=0.8, reasoning="r",
        reasoning_type=REASONING_CONTRIBUTING, risk_level=RISK_MEDIUM,
        key_drivers=[_drv("d1", cat="credit")],
    )
    out = agent_output_from_vote(vote, consensus_direction="long").to_dict()
    expected_keys = {
        "agent", "role", "stance", "confidence", "weight",
        "reasoning", "reasoning_type",
        "supporting_factors", "concerns",
        "invalidation", "source_categories",
        "expected_edge_bps", "risk_level",
    }
    assert set(out.keys()) == expected_keys
    assert out["confidence"] == 50
    assert out["weight"] == 0.8
