"""MITS Phase 18.B — Counterfactual Replayer unit tests.

Covers the three variations + bundle helper + dataclass round-trip.
Seeds real Trade + DecisionProvenance rows in the test DB so the
counterfactual module's load helpers actually exercise the persisted
shape (the same shape the live engine writes).

Honesty guardrails the suite exercises:

  * Sizing CF on open trade → None
  * Sizing CF with no sizing_chain_json → None
  * Policy CF on a rule that didn't block → None
  * Policy CF on a real blocker → eligible_with_override flips
  * Policy CF surfaces other_blockers_still_firing when multiple hard
    vetoes fired concurrently
  * Consensus CF deterministic: call twice, identical result
  * Consensus CF on row without agent_outputs → None
  * Bundle's compute_all_counterfactuals never raises on edge cases
  * to_dict round-trips through json
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest

from backend.bot.learning.counterfactual import (
    ALLOWED_STANCES,
    CF_NOTE_MISSING_PROV,
    CF_NOTE_TRADE_NOT_CLOSED,
    DEFAULT_SIZING_FACTORS,
    CounterfactualResult,
    PolicyCounterfactual,
    SIZING_NOTE,
    SizingCounterfactual,
    compute_all_counterfactuals,
    consensus_counterfactual,
    policy_counterfactual,
    sizing_counterfactual,
)
from backend.db import session_scope
from backend.models.decision_provenance import DecisionProvenance
from backend.models.trade import Trade


pytestmark = [pytest.mark.unit]


# ── Helpers ──────────────────────────────────────────────────────────


def _seed_closed_stock_trade(
    *,
    ticker: str = "AAPL", pnl: float = 100.0,
    quantity: float = 10.0, price: float = 100.0,
    with_sizing_chain: bool = True,
    status: str = "closed",
) -> int:
    """Create a Trade + linked DecisionProvenance row. Returns the
    provenance id so the test can drive a counterfactual against it.

    The sizing_chain JSON shape mirrors what
    ``backend.bot.execution.sizing_chain`` writes at fill time.
    """
    with session_scope() as s:
        sizing_chain = None
        if with_sizing_chain:
            sizing_chain = json.dumps({
                "base_qty": float(quantity),
                "steps": [
                    {
                        "name": "risk_manager",
                        "input": float(quantity), "factor": 1.0,
                        "output": float(quantity),
                    },
                ],
                "final_qty": float(quantity),
                "rounded_final": float(quantity),
                "captured_at": datetime.utcnow().isoformat(),
            })
        trade = Trade(
            ticker=ticker, action="BUY", quantity=quantity, price=price,
            strategy="test_strategy", signal_source="live_engine",
            confidence=0.7, reason="seed",
            paper=1, pnl=pnl, status=status,
            instrument="stock",
            sizing_chain_json=sizing_chain,
        )
        s.add(trade)
        s.flush()
        prov = DecisionProvenance(
            trade_id=int(trade.id),
            event_status="submitted",
            ticker=ticker,
            decision_timestamp=datetime.utcnow() - timedelta(days=1),
            cycle_id=f"seed-{trade.id}",
        )
        s.add(prov)
        s.flush()
        return int(prov.id)


def _seed_blocked_prov(
    *, blockers: List[Dict[str, Any]],
    ticker: str = "MSFT",
) -> int:
    """Seed a provenance row with a synthetic ``policy_result_json``
    listing the supplied blockers. No linked Trade — these are the
    "decision was blocked before the order went out" rows."""
    with session_scope() as s:
        policy_result = {
            "eligible": False,
            "blocking_factors": list(blockers),
            "soft_penalties_total_pct": 0.0,
            "evaluated_at": datetime.utcnow().isoformat(),
            "rule_evaluations": [],
        }
        prov = DecisionProvenance(
            trade_id=None,
            event_status=str(blockers[0]["rule"]),
            ticker=ticker,
            decision_timestamp=datetime.utcnow() - timedelta(days=1),
            cycle_id="seed-blocked",
            policy_result_json=json.dumps(policy_result),
        )
        s.add(prov)
        s.flush()
        return int(prov.id)


def _seed_prov_with_council(
    *, ticker: str = "NVDA",
    agent_outputs: List[Dict[str, Any]],
    consensus: Dict[str, Any],
) -> int:
    """Seed a provenance row that carries persisted agent_outputs +
    consensus JSON (the shape ``run_consensus`` writes)."""
    with session_scope() as s:
        prov = DecisionProvenance(
            trade_id=None,
            event_status="signal_only",
            ticker=ticker,
            decision_timestamp=datetime.utcnow() - timedelta(days=1),
            cycle_id="seed-council",
            agent_outputs_json=json.dumps(agent_outputs),
            consensus_json=json.dumps(consensus),
        )
        s.add(prov)
        s.flush()
        return int(prov.id)


def _output(
    agent: str, stance: str, confidence: int = 70,
    reasoning_type: str = "contributing",
    supporting: List[str] = None, concerns: List[str] = None,
) -> Dict[str, Any]:
    """Mirror of the AgentOutput shape persisted to agent_outputs_json
    (contracts_v2 — int confidence 0..100)."""
    return {
        "agent": agent,
        "role": "test",
        "stance": stance,
        "confidence": int(confidence),
        "weight": 1.0,
        "reasoning": "synthetic",
        "reasoning_type": reasoning_type,
        "supporting_factors": list(supporting or []),
        "concerns": list(concerns or []),
        "source_categories": ["macro_liquidity"] if (supporting or concerns) else [],
    }


# ── 1. Sizing CF — happy path ────────────────────────────────────────


def test_sizing_cf_on_closed_trade_with_chain_emits_linear_curve():
    prov_id = _seed_closed_stock_trade(pnl=200.0)
    cf = sizing_counterfactual(prov_id)
    assert cf is not None
    assert cf.original_pnl == pytest.approx(200.0)
    assert cf.original_factor == 1.0
    assert cf.note == SIZING_NOTE
    # Default factors are 0.5/1.0/1.5/2.0 → 100/200/300/400.
    pairs = dict(cf.pnl_curve)
    assert pairs[0.5] == pytest.approx(100.0)
    assert pairs[1.0] == pytest.approx(200.0)
    assert pairs[1.5] == pytest.approx(300.0)
    assert pairs[2.0] == pytest.approx(400.0)


# ── 2. Sizing CF — custom factors honored ────────────────────────────


def test_sizing_cf_honors_custom_factor_list():
    prov_id = _seed_closed_stock_trade(pnl=50.0)
    cf = sizing_counterfactual(prov_id, factors=[0.25, 3.0])
    assert cf is not None
    assert cf.factors == [0.25, 3.0]
    pairs = dict(cf.pnl_curve)
    assert pairs[0.25] == pytest.approx(12.5)
    assert pairs[3.0] == pytest.approx(150.0)


# ── 3. Sizing CF — open trade returns None ───────────────────────────


def test_sizing_cf_on_open_trade_returns_none():
    prov_id = _seed_closed_stock_trade(pnl=100.0, status="open")
    assert sizing_counterfactual(prov_id) is None


# ── 4. Sizing CF — no sizing_chain_json returns None ─────────────────


def test_sizing_cf_without_sizing_chain_returns_none():
    prov_id = _seed_closed_stock_trade(pnl=100.0, with_sizing_chain=False)
    assert sizing_counterfactual(prov_id) is None


# ── 5. Policy CF — rule didn't block → None ──────────────────────────


def test_policy_cf_on_rule_that_did_not_block_returns_none():
    prov_id = _seed_blocked_prov(blockers=[
        {
            "category": "risk", "rule": "kill_switch_active",
            "severity": "hard", "reason": "operator hold",
            "evidence": {}, "sizing_penalty_pct": 0.0,
            "legacy_status": "kill_switch_active",
            "override_event_reason": True,
        },
    ])
    # Asking about a rule that's NOT in blockers must return None.
    assert policy_counterfactual(prov_id, "low_confidence") is None


# ── 6. Policy CF — single blocker → eligible_with_override ───────────


def test_policy_cf_single_blocker_flips_eligible_with_override():
    prov_id = _seed_blocked_prov(blockers=[
        {
            "category": "risk", "rule": "kill_switch_active",
            "severity": "hard", "reason": "operator hold",
            "evidence": {}, "sizing_penalty_pct": 0.0,
            "legacy_status": "kill_switch_active",
            "override_event_reason": True,
        },
    ])
    cf = policy_counterfactual(prov_id, "kill_switch_active")
    assert cf is not None
    assert cf.rule_overridden == "kill_switch_active"
    assert cf.original_headline_blocker == "kill_switch_active"
    assert cf.new_headline_blocker is None
    assert cf.eligible_with_override is True
    assert cf.other_blockers_still_firing == []


# ── 7. Policy CF — multiple blockers surface next headline ───────────


def test_policy_cf_with_concurrent_blockers_lists_remaining():
    """When the engine collects every concurrent veto, overriding one
    must leave the next-in-list HARD blocker as the new headline."""
    prov_id = _seed_blocked_prov(blockers=[
        {
            "category": "risk", "rule": "kill_switch_active",
            "severity": "hard", "reason": "operator hold",
            "evidence": {}, "sizing_penalty_pct": 0.0,
            "legacy_status": "kill_switch_active",
            "override_event_reason": True,
        },
        {
            "category": "strategy", "rule": "low_confidence",
            "severity": "hard", "reason": "consensus too thin",
            "evidence": {"confidence": 0.32},
            "sizing_penalty_pct": 0.0,
            "legacy_status": "low_confidence",
            "override_event_reason": False,
        },
    ])
    cf = policy_counterfactual(prov_id, "kill_switch_active")
    assert cf is not None
    assert cf.eligible_with_override is False
    assert cf.new_headline_blocker == "low_confidence"
    assert "low_confidence" in cf.other_blockers_still_firing


# ── 8. Consensus CF — deterministic re-aggregation ───────────────────


def test_consensus_cf_is_deterministic_on_repeated_calls():
    """Re-running consensus aggregation MUST be deterministic — the
    16.B replay invariant must hold across counterfactual layers."""
    outputs = [
        _output("market", "buy", 70,
                       supporting=["spy_above_50dma"]),
        _output("microstructure", "buy", 65,
                       supporting=["thin_offer"]),
        _output("macro", "abstain", 0,
                       reasoning_type="insufficient_signal"),
        _output("simulator", "abstain", 0,
                       reasoning_type="insufficient_signal"),
        _output("portfolio_risk", "buy", 60,
                       supporting=["fits_correlation_cap"]),
    ]
    consensus = {
        "stance": "buy", "confidence": 0.65,
        "recommendation": "execute", "size_multiplier": 0.85,
        "quorum_required": 3, "quorum_met": True,
    }
    prov_id = _seed_prov_with_council(
        agent_outputs=outputs, consensus=consensus,
    )
    a = consensus_counterfactual(
        prov_id, agent="simulator", new_stance="buy", new_confidence=70,
    )
    b = consensus_counterfactual(
        prov_id, agent="simulator", new_stance="buy", new_confidence=70,
    )
    assert a is not None and b is not None
    assert a.new_consensus == b.new_consensus
    assert a.new_consensus["stance"] in {"buy", "sell", "abstain", "hold"}


# ── 9. Consensus CF — empty agent_outputs returns None ───────────────


def test_consensus_cf_on_row_without_outputs_returns_none():
    prov_id = _seed_blocked_prov(blockers=[
        {
            "category": "risk", "rule": "kill_switch_active",
            "severity": "hard", "reason": "hold",
            "evidence": {}, "sizing_penalty_pct": 0.0,
            "legacy_status": "kill_switch_active",
            "override_event_reason": True,
        },
    ])
    assert consensus_counterfactual(
        prov_id, agent="market", new_stance="buy", new_confidence=70,
    ) is None


# ── 10. Consensus CF — unknown agent returns None ────────────────────


def test_consensus_cf_unknown_agent_name_returns_none():
    outputs = [
        _output("market", "buy", 70, supporting=["x"]),
    ]
    consensus = {"stance": "buy", "confidence": 0.70, "quorum_required": 1}
    prov_id = _seed_prov_with_council(
        agent_outputs=outputs, consensus=consensus,
    )
    assert consensus_counterfactual(
        prov_id, agent="nonexistent", new_stance="buy", new_confidence=70,
    ) is None


# ── 11. Consensus CF — invalid stance returns None ───────────────────


def test_consensus_cf_invalid_stance_returns_none():
    outputs = [_output("market", "buy", 70, supporting=["x"])]
    consensus = {"stance": "buy", "confidence": 0.70, "quorum_required": 1}
    prov_id = _seed_prov_with_council(
        agent_outputs=outputs, consensus=consensus,
    )
    assert consensus_counterfactual(
        prov_id, agent="market", new_stance="LONG", new_confidence=70,
    ) is None


# ── 12. to_dict round-trip ───────────────────────────────────────────


def test_dataclasses_round_trip_through_to_dict_and_json():
    sizing = SizingCounterfactual(
        factors=[0.5, 1.0], original_pnl=10.0, original_factor=1.0,
        pnl_curve=[(0.5, 5.0), (1.0, 10.0)],
    )
    d = sizing.to_dict()
    s = json.dumps(d)
    reloaded = json.loads(s)
    assert reloaded["original_pnl"] == 10.0
    assert reloaded["pnl_curve"][0] == [0.5, 5.0]
    assert reloaded["note"] == SIZING_NOTE

    policy = PolicyCounterfactual(
        rule_overridden="low_confidence",
        original_headline_blocker="low_confidence",
        new_headline_blocker=None, eligible_with_override=True,
        other_blockers_still_firing=[],
    )
    pd = policy.to_dict()
    assert json.loads(json.dumps(pd))["eligible_with_override"] is True


# ── 13. compute_all_counterfactuals — missing prov ───────────────────


def test_compute_all_on_missing_prov_returns_note_not_exception():
    res = compute_all_counterfactuals(999_999_999)
    assert isinstance(res, CounterfactualResult)
    assert res.sizing is None
    assert res.policy is None
    assert res.consensus is None
    assert CF_NOTE_MISSING_PROV in res.notes


# ── 14. compute_all_counterfactuals — bundle on real row ─────────────


def test_compute_all_on_closed_trade_returns_sizing_panel():
    prov_id = _seed_closed_stock_trade(pnl=42.0)
    res = compute_all_counterfactuals(prov_id)
    assert isinstance(res, CounterfactualResult)
    assert res.sizing is not None
    assert res.sizing.original_pnl == pytest.approx(42.0)
    # No policy / consensus blob seeded → those are None with notes.
    assert res.policy is None
    assert res.consensus is None
    bundled = res.to_dict()
    assert bundled["provenance_id"] == prov_id
    assert bundled["sizing"]["note"] == SIZING_NOTE


# ── 15. compute_all_counterfactuals — flagged for blocked row ────────


def test_compute_all_on_blocked_row_picks_headline_for_policy_cf():
    prov_id = _seed_blocked_prov(blockers=[
        {
            "category": "risk", "rule": "kill_switch_active",
            "severity": "hard", "reason": "operator hold",
            "evidence": {}, "sizing_penalty_pct": 0.0,
            "legacy_status": "kill_switch_active",
            "override_event_reason": True,
        },
    ])
    res = compute_all_counterfactuals(prov_id)
    assert res.policy is not None
    assert res.policy.rule_overridden == "kill_switch_active"
    assert res.policy.eligible_with_override is True
    # Trade not present → sizing slot is None + note explains it.
    assert res.sizing is None
    assert CF_NOTE_TRADE_NOT_CLOSED in res.notes


# ── 16. Allowed stances are advertised ───────────────────────────────


def test_allowed_stances_exposed_for_route_validation():
    assert "buy" in ALLOWED_STANCES
    assert "sell" in ALLOWED_STANCES
    assert "abstain" in ALLOWED_STANCES
    assert "hold" in ALLOWED_STANCES
    assert "LONG" not in ALLOWED_STANCES


# ── 17. Default sizing factors are the operator's standard 4 ─────────


def test_default_sizing_factors_match_spec():
    assert tuple(DEFAULT_SIZING_FACTORS) == (0.5, 1.0, 1.5, 2.0)
