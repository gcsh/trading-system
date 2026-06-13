"""MITS Phase 15.D — multi-axis ConfidenceBreakdown unit tests.

Exercises every code path in ``_compute_confidence_breakdown``:
- structured votes across all 6 axes
- silent microstructure ⇒ options axis red
- no analog_cluster + no simulator_verdict ⇒ both red
- analog_cluster with pre-computed analog_win_rate ⇒ historical_analog populated
- silent simulator vote + verdict has conviction_score ⇒ simulator falls
  back to the verdict
"""
from __future__ import annotations

from backend.bot.agents import (
    AgentVote,
    STANCE_ABSTAIN,
    STANCE_BUY,
    _compute_confidence_breakdown,
)
from backend.bot.agents.contract import (
    DIRECTION_LONG,
    KeyDriver,
    REASONING_CONTRIBUTING,
    REASONING_INSUFFICIENT_SIGNAL,
    RISK_MEDIUM,
)


def _kd(desc: str, cat: str, *, weight: float = 0.7) -> KeyDriver:
    return KeyDriver(description=desc, source_category=cat,
                     direction=DIRECTION_LONG, weight=weight)


def _vote(agent: str, *, conf: float, weight: float = 1.0,
          drivers, silent: bool = False) -> AgentVote:
    if silent:
        return AgentVote(
            agent=agent, role=agent, stance=STANCE_ABSTAIN,
            confidence=0.0, weight=weight, reasoning="silent",
            reasoning_type=REASONING_INSUFFICIENT_SIGNAL,
            risk_level=RISK_MEDIUM, key_drivers=[],
        )
    return AgentVote(
        agent=agent, role=agent, stance=STANCE_BUY,
        confidence=conf, weight=weight, reasoning="x",
        reasoning_type=REASONING_CONTRIBUTING,
        risk_level=RISK_MEDIUM, key_drivers=drivers,
    )


def _structured_full_panel():
    """Seven structured votes covering every analytical axis."""
    return [
        _vote("market", conf=0.70,
              drivers=[_kd("breadth bullish", "breadth")]),
        _vote("macro", conf=0.60,
              drivers=[_kd("NFCI loose", "macro_liquidity")]),
        _vote("microstructure", conf=0.80,
              drivers=[
                  _kd("call iv crush 30%", "volatility", weight=0.6),
                  _kd("dark pool flow burst", "microstructure_flow",
                      weight=0.8),
              ]),
        _vote("mechanical_trend", conf=0.55,
              drivers=[_kd("price > EMA200", "price_structure")]),
        _vote("portfolio_risk", conf=0.50,
              drivers=[_kd("low theme heat", "portfolio_state")]),
        _vote("thesis_health", conf=0.0, silent=True, drivers=[]),
        _vote("simulator", conf=0.65,
              drivers=[_kd("monte carlo > 0", "price_structure")]),
    ]


def test_all_axes_populated_when_full_panel_votes():
    votes = _structured_full_panel()
    analog = {"cohort_size": 24, "analog_win_rate": 0.62, "analogs": []}
    cb = _compute_confidence_breakdown(votes, analog_cluster=analog)
    d = cb.to_dict()

    # All 6 axes have nonzero confidence
    for ax in ("market_structure", "technical", "options",
               "historical_analog", "simulator", "macro"):
        assert d[ax] > 0.0, f"{ax} should be > 0"

    # market_structure receives market + macro ⇒ green
    assert d["axis_health"]["market_structure"] == "green"
    assert d["axis_n"]["market_structure"] == 2

    # technical receives microstructure + mechanical_trend ⇒ green
    assert d["axis_health"]["technical"] == "green"
    assert d["axis_n"]["technical"] == 2

    # options receives microstructure base vote + the iv/flow-derived
    # sub-confidence — two entries from one agent ⇒ green by count.
    assert d["axis_health"]["options"] == "green"
    assert d["axis_n"]["options"] == 2

    # macro has only the macro agent ⇒ yellow
    assert d["axis_health"]["macro"] == "yellow"

    # simulator vote present ⇒ yellow (single contributor)
    assert d["axis_health"]["simulator"] == "yellow"

    # historical_analog comes from analog_cluster ⇒ yellow (single source)
    assert d["axis_health"]["historical_analog"] == "yellow"
    assert d["historical_analog"] == 0.62

    # Composite is the mean of the six nonzero axes
    nonzero = [d[ax] for ax in ("market_structure", "technical", "options",
                                  "historical_analog", "simulator", "macro")
               if d[ax] > 0]
    expected = round(sum(nonzero) / len(nonzero), 4)
    assert d["composite"] == expected


def test_options_axis_red_when_microstructure_silent():
    """Silent microstructure + no other agent feeds options ⇒ red."""
    votes = [
        _vote("market", conf=0.7,
              drivers=[_kd("breadth", "breadth")]),
        _vote("mechanical_trend", conf=0.5,
              drivers=[_kd("trend", "price_structure")]),
        _vote("microstructure", conf=0.0, silent=True, drivers=[]),
    ]
    cb = _compute_confidence_breakdown(votes)
    d = cb.to_dict()
    assert d["options"] == 0.0
    assert d["axis_health"]["options"] == "red"
    assert d["axis_n"]["options"] == 0
    # Microstructure being silent also leaves technical with only
    # mechanical_trend ⇒ yellow.
    assert d["axis_health"]["technical"] == "yellow"


def test_red_axes_when_no_analog_no_simulator_verdict():
    """analog_cluster=None + no simulator vote + no verdict ⇒ both red."""
    votes = [
        _vote("market", conf=0.6,
              drivers=[_kd("breadth", "breadth")]),
    ]
    cb = _compute_confidence_breakdown(
        votes, analog_cluster=None, simulator_verdict=None,
    )
    d = cb.to_dict()
    assert d["historical_analog"] == 0.0
    assert d["axis_health"]["historical_analog"] == "red"
    assert d["simulator"] == 0.0
    assert d["axis_health"]["simulator"] == "red"


def test_historical_analog_populated_from_precomputed_win_rate():
    votes = []
    analog = {"cohort_size": 12, "analog_win_rate": 0.41, "analogs": []}
    cb = _compute_confidence_breakdown(votes, analog_cluster=analog)
    d = cb.to_dict()
    assert d["historical_analog"] == 0.41
    assert d["axis_health"]["historical_analog"] == "yellow"


def test_historical_analog_computed_from_realized_returns():
    votes = []
    analog = {
        "cohort_size": 4,
        "analogs": [
            {"realized_return_pct": 0.05},
            {"realized_return_pct": -0.02},
            {"realized_return_pct": 0.01},
            {"realized_return_pct": 0.03},
        ],
    }
    cb = _compute_confidence_breakdown(votes, analog_cluster=analog)
    # 3 of 4 hits were positive ⇒ 0.75
    assert cb.to_dict()["historical_analog"] == 0.75


def test_simulator_axis_falls_back_to_verdict_when_vote_silent():
    votes = [
        _vote("simulator", conf=0.0, silent=True, drivers=[]),
        _vote("market", conf=0.6,
              drivers=[_kd("breadth", "breadth")]),
    ]
    verdict = {"conviction_score": 0.72}
    cb = _compute_confidence_breakdown(votes, simulator_verdict=verdict)
    d = cb.to_dict()
    assert d["simulator"] == 0.72
    # Single source (the verdict) ⇒ yellow
    assert d["axis_health"]["simulator"] == "yellow"
    assert d["axis_n"]["simulator"] == 1
