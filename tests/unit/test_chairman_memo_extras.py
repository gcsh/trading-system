"""MITS Phase 16.B — Chairman Decision Memo extras.

Pins:
  • kill_condition: highest-weight supporter invalidator, verbatim,
    falling back to critical_risk
  • structured_why: top-5 supporter driver descriptions, verbatim
  • main_risk: primary dissenter's first driver, or critical_risk fallback
  • confidence_pct: int round(conviction * 100)
  • All four are lossless — every char comes from an agent input
"""
from __future__ import annotations

from backend.bot.agents import (
    AgentVote,
    STANCE_ABSTAIN,
    STANCE_BUY,
    STANCE_SELL,
)
from backend.bot.agents.chairman import chairman_review
from backend.bot.agents.contract import (
    DIRECTION_LONG,
    DIRECTION_SHORT,
    KeyDriver,
    REASONING_CONTRIBUTING,
    REASONING_DISSENTING,
)


def _kd(desc, cat="credit", direction=DIRECTION_LONG):
    return KeyDriver(
        description=desc, source_category=cat, direction=direction,
        weight=0.6, time_sensitive=False,
    )


def _supporter(name, conf=0.7, weight=1.0, drivers=None, invalidators=None,
               risk="MEDIUM"):
    return AgentVote(
        agent=name, role=name.upper(), stance=STANCE_BUY,
        confidence=conf, weight=weight, reasoning=f"{name} supports",
        reasoning_type=REASONING_CONTRIBUTING, risk_level=risk,
        invalidators=invalidators or [],
        key_drivers=drivers or [_kd(f"{name} driver")],
    )


def _dissenter(name, conf=0.6, weight=1.0, drivers=None):
    return AgentVote(
        agent=name, role=name.upper(), stance=STANCE_SELL,
        confidence=conf, weight=weight, reasoning=f"{name} dissents",
        reasoning_type=REASONING_DISSENTING, risk_level="HIGH",
        invalidators=["dissent invalid"],
        key_drivers=drivers or [
            _kd(f"{name} bear", cat="credit", direction=DIRECTION_SHORT),
        ],
    )


def test_kill_condition_is_highest_weight_supporter_invalidator():
    """Two supporters with invalidators; the higher confidence × weight
    wins. Verbatim — chairman emits exactly what the agent wrote."""
    votes = [
        _supporter("market", conf=0.9, weight=1.0,
                   invalidators=["SPY breaks 50dma", "extra"]),
        _supporter("macro", conf=0.5, weight=1.0,
                   invalidators=["HY blows out"]),
        _supporter("micro", conf=0.6, weight=1.0,
                   drivers=[_kd("flow", cat="microstructure_flow")]),
    ]
    rep = chairman_review(
        votes=votes, consensus_stance=STANCE_BUY,
        abstain_stance=STANCE_ABSTAIN, quorum_met=True,
        quorum_count=3, quorum_required=2,
    )
    # 0.9*1 > 0.5*1 ⇒ market's first invalidator wins.
    assert rep.kill_condition == "market: SPY breaks 50dma"


def test_kill_condition_falls_back_to_critical_risk():
    """No supporter invalidators but a HIGH-risk supporter exists ⇒
    critical_risk surfaces as the kill_condition (still verbatim)."""
    high_risk = _supporter(
        "macro", conf=0.7, weight=1.0,
        invalidators=[],
        risk="HIGH",
    )
    # critical_risk is built from the HIGH-risk vote's invalidators, so
    # we have to provide one; the supporter-invalidator branch only
    # collects invalidators from supporters specifically.
    # Construct a HIGH-risk supporter with an invalidator so
    # critical_risk is non-empty AND that path also drives kill_condition
    # (since supporter_invs is also non-empty, this case actually goes
    # through the primary branch). To exercise the fallback we need a
    # supporter with NO invalidators + a HIGH-risk vote that has one.
    # The chairman's critical_risk logic counts BOTH supporters and
    # counters with HIGH risk_level. So add a HIGH-risk dissenter for
    # the fallback path.
    sup_no_inv = _supporter("market", conf=0.8, weight=1.0, invalidators=[])
    bear = _dissenter("macro")  # HIGH risk, has invalidator
    votes = [sup_no_inv, bear]
    rep = chairman_review(
        votes=votes, consensus_stance=STANCE_BUY,
        abstain_stance=STANCE_ABSTAIN, quorum_met=True,
        quorum_count=2, quorum_required=2,
    )
    # No supporter invalidators ⇒ kill_condition falls back to
    # critical_risk, which is the HIGH-risk vote's first invalidator.
    assert rep.kill_condition == "macro: dissent invalid"
    assert rep.critical_risk == "macro: dissent invalid"


def test_structured_why_lists_top_supporter_drivers_verbatim():
    votes = [
        _supporter(
            "market", conf=0.9, weight=1.0,
            drivers=[_kd("breadth thrust 0.92")],
        ),
        _supporter(
            "micro", conf=0.6, weight=1.0,
            drivers=[_kd("dark pool sweep", cat="microstructure_flow")],
        ),
        _supporter(
            "macro", conf=0.5, weight=1.0,
            drivers=[_kd("NFCI loose", cat="macro_liquidity")],
        ),
    ]
    rep = chairman_review(
        votes=votes, consensus_stance=STANCE_BUY,
        abstain_stance=STANCE_ABSTAIN, quorum_met=True,
        quorum_count=3, quorum_required=2,
    )
    # Sorted by confidence × weight: market > micro > macro.
    assert rep.structured_why == [
        "market: breadth thrust 0.92",
        "micro: dark pool sweep",
        "macro: NFCI loose",
    ]


def test_structured_why_capped_at_five():
    votes = [
        _supporter(
            f"sup{i}", conf=0.7 + i * 0.01, weight=1.0,
            drivers=[_kd(f"driver {i}")],
        )
        for i in range(8)
    ]
    rep = chairman_review(
        votes=votes, consensus_stance=STANCE_BUY,
        abstain_stance=STANCE_ABSTAIN, quorum_met=True,
        quorum_count=8, quorum_required=2,
    )
    assert len(rep.structured_why) == 5
    # Highest-weight (sup7 .. sup3) come first.
    assert rep.structured_why[0].startswith("sup7:")


def test_main_risk_is_primary_dissenter_first_driver():
    votes = [
        _supporter("market", conf=0.7, weight=1.0),
        _dissenter("macro", conf=0.6, weight=1.0,
                   drivers=[_kd("HY blowout", cat="credit",
                                 direction=DIRECTION_SHORT)]),
        _dissenter("devil", conf=0.4, weight=1.0,
                   drivers=[_kd("vol spike", cat="volatility",
                                 direction=DIRECTION_SHORT)]),
    ]
    rep = chairman_review(
        votes=votes, consensus_stance=STANCE_BUY,
        abstain_stance=STANCE_ABSTAIN, quorum_met=True,
        quorum_count=3, quorum_required=2,
    )
    # macro is the primary dissenter (higher confidence*weight than devil).
    assert rep.dissent.primary_dissenter == "macro"
    assert rep.main_risk == "macro: HY blowout"


def test_main_risk_falls_back_to_critical_risk_when_no_dissenter():
    """No dissenter but HIGH-risk supporter posts an invalidator ⇒
    main_risk == critical_risk."""
    votes = [
        _supporter("market", conf=0.8, weight=1.0,
                   invalidators=["breaks support"], risk="HIGH"),
        _supporter("macro", conf=0.5, weight=1.0),
    ]
    rep = chairman_review(
        votes=votes, consensus_stance=STANCE_BUY,
        abstain_stance=STANCE_ABSTAIN, quorum_met=True,
        quorum_count=2, quorum_required=2,
    )
    assert rep.dissent.primary_dissenter is None
    assert rep.critical_risk == "market: breaks support"
    assert rep.main_risk == "market: breaks support"


def test_confidence_pct_is_integer_percent():
    votes = [
        _supporter("market", conf=0.7, weight=1.0),
        _supporter("macro", conf=0.5, weight=1.0),
    ]
    rep = chairman_review(
        votes=votes, consensus_stance=STANCE_BUY,
        abstain_stance=STANCE_ABSTAIN, quorum_met=True,
        quorum_count=2, quorum_required=2,
    )
    # Weighted mean of 0.7 + 0.5 (equal weights) = 0.6
    assert rep.confidence_pct == 60
    assert rep.conviction == 0.6
