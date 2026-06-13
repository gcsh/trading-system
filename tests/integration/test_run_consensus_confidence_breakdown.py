"""MITS Phase 15 follow-up - integration test for run_consensus ->
ConfidenceBreakdown end-to-end. Uses the real AgentVote builder and
verifies the full council path populates confidence_breakdown."""
from __future__ import annotations

from typing import Any, Dict

import pytest

from backend.bot.agents import run_consensus


pytestmark = [pytest.mark.integration]


# Nine documented fields ConfidenceBreakdown.to_dict() must surface.
_BREAKDOWN_KEYS = {
    "market_structure", "technical", "options",
    "historical_analog", "simulator", "macro",
    "composite", "axis_health", "axis_n",
}

# Six analytical axes that axis_health / axis_n key on.
_AXES = {"market_structure", "technical", "options",
         "historical_analog", "simulator", "macro"}

_HEALTH_STATES = {"green", "yellow", "red"}


def _bullish_long_call_context() -> Dict[str, Any]:
    """Realistic call-buy context with every council input populated so
    market / macro / microstructure / mechanical_trend / simulator all
    have evidence to vote on."""
    return {
        "ticker": "NVDA",
        "action": "BUY_CALL",
        "strategy": "trend_pullback",
        "analytics": {
            "regime": {
                "trend": "bullish", "volatility": "normal",
                "gamma": "long_gamma", "momentum": "expanding",
                "label": "bullish - normal-vol - long gamma",
            },
            "features": {
                "trend_bias": 0.5, "flow_bullishness": 0.4,
                "premarket_bullish_sweeps": 0.6,
                "dealer_regime": "long_gamma", "hedging_pressure": "normal",
                "iv_rank": 30, "pinning_probability": 0.1,
                "earnings_days": 30, "vix": 14, "news_sentiment": 0.3,
                "volume_ratio": 1.4,
            },
        },
        "snapshot": {
            "spy_trend": "bullish", "vix": 14,
            "volume": 1_400_000, "avg_volume": 1_000_000,
            "price": 100.0, "rsi": 60.0,
            "ema50": 95.0, "ema200": 90.0,
        },
        "cross_asset": {"equities": "risk_on", "volatility": "compressed"},
        "portfolio_risk": {
            "net_beta": 0.5, "drawdown_pct": 0.01,
            "top_theme": "AI infra", "top_theme_pct": 0.18,
            "concentration_flags": [],
        },
        "cohort": {"win_rate": 0.62, "closed_count": 35},
    }


def test_run_consensus_populates_confidence_breakdown_end_to_end():
    """Full council path: every documented key present, composite > 0,
    axis_health/axis_n maps cover all six axes with valid values."""
    context = _bullish_long_call_context()

    consensus = run_consensus(context)
    cb = consensus.confidence_breakdown

    assert isinstance(cb, dict)
    assert cb, "confidence_breakdown must not be empty after run_consensus"
    assert set(cb.keys()) == _BREAKDOWN_KEYS, (
        f"unexpected keys: {set(cb.keys()) ^ _BREAKDOWN_KEYS}"
    )

    assert cb["composite"] > 0.0, (
        "at least one axis must contribute to the composite "
        f"when the full council votes; got {cb['composite']}"
    )

    health = cb["axis_health"]
    assert isinstance(health, dict)
    assert set(health.keys()) == _AXES, (
        f"axis_health must cover all six axes; got {sorted(health.keys())}"
    )
    for axis, state in health.items():
        assert state in _HEALTH_STATES, (
            f"axis_health[{axis!r}] = {state!r} not in {_HEALTH_STATES}"
        )

    n_map = cb["axis_n"]
    assert isinstance(n_map, dict)
    assert set(n_map.keys()) == _AXES, (
        f"axis_n must cover all six axes; got {sorted(n_map.keys())}"
    )
    for axis, n in n_map.items():
        assert isinstance(n, int), f"axis_n[{axis!r}] must be int, got {type(n)}"
        assert n >= 0, f"axis_n[{axis!r}] must be >= 0, got {n}"


def test_options_axis_health_discriminates_when_microstructure_silent():
    """Stripping every microstructure input (no flow, no IV, no volume,
    no options action) forces microstructure to abstain silently. With
    no other agent feeding the options axis, axis_health['options'] must
    NOT be green - proving the health score actually discriminates."""
    context = _bullish_long_call_context()

    # Switch off the options action so microstructure's is_options branch
    # cannot inflate the options axis through IV/pin drivers.
    context["action"] = "BUY"
    # Drop every microstructure-relevant feature.
    features = context["analytics"]["features"]
    for k in ("flow_bullishness", "premarket_bullish_sweeps",
              "iv_rank", "pinning_probability", "volume_ratio",
              "dealer_regime", "hedging_pressure"):
        features.pop(k, None)
    # Drop volume signals from the snapshot so has_vol is False too.
    snap = context["snapshot"]
    snap.pop("volume", None)
    snap.pop("avg_volume", None)

    consensus = run_consensus(context)
    cb = consensus.confidence_breakdown

    assert cb["axis_health"]["options"] in {"yellow", "red"}, (
        "options axis must not be green when microstructure is silent "
        f"and no other agent feeds the axis; got {cb['axis_health']['options']!r}"
    )
    # And the microstructure agent did in fact go silent in this scenario.
    micro = next(v for v in consensus.votes if v["agent"] == "microstructure")
    assert micro["reasoning_type"] == "insufficient_signal", (
        f"expected silent microstructure vote, got {micro['reasoning_type']!r}"
    )


def test_analog_cluster_lifts_historical_analog_axis():
    """An analog_cluster on the context with a pre-computed win-rate
    must propagate into confidence_breakdown['historical_analog'] and
    keep that axis off red."""
    context = _bullish_long_call_context()
    context["analog_cluster"] = {"cohort_size": 25, "analog_win_rate": 0.58}

    consensus = run_consensus(context)
    cb = consensus.confidence_breakdown

    assert cb["historical_analog"] > 0.0, (
        "analog_cluster.analog_win_rate must lift the historical_analog axis; "
        f"got {cb['historical_analog']}"
    )
    assert cb["axis_health"]["historical_analog"] != "red", (
        "historical_analog axis_health must not be red once the cluster "
        f"contributes; got {cb['axis_health']['historical_analog']!r}"
    )
    assert cb["axis_n"]["historical_analog"] >= 1
