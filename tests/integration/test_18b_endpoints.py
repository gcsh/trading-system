"""MITS Phase 18.B — Counterfactual endpoint integration tests.

Drives the four new ``/learning/counterfactual/*`` endpoints +
verifies the decision cockpit response carries the new
``counterfactuals`` key.

Honesty + correctness assertions:

  1. GET /learning/counterfactual/{prov_id} returns 200 with the
     bundle shape (sizing/policy/consensus/notes).
  2. POST /learning/counterfactual/{prov_id}/sizing accepts a factor
     list and returns the linear-scaled curve.
  3. POST /learning/counterfactual/{prov_id}/policy accepts a
     rule_name and returns the override verdict.
  4. POST /learning/counterfactual/{prov_id}/consensus accepts the
     agent + new_stance + new_confidence triple.
  5. The counterfactual_replays cache is populated — a second GET
     returns the same cached row id.
  6. /decision/cockpit/{prov_id} response carries the new
     ``counterfactuals`` key.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.db import session_scope
from backend.main import app
from backend.models.counterfactual_replay import CounterfactualReplay
from backend.models.decision_provenance import DecisionProvenance
from backend.models.trade import Trade


pytestmark = [pytest.mark.integration]


def _seed_closed_trade_with_provenance(
    *, ticker: str = "AAPL", pnl: float = 250.0,
) -> int:
    """Insert one closed Trade + linked DecisionProvenance row with a
    sizing_chain JSON. Returns the provenance id."""
    with session_scope() as s:
        chain = {
            "base_qty": 10.0,
            "steps": [{
                "name": "risk_manager", "input": 10.0,
                "factor": 1.0, "output": 10.0,
            }],
            "final_qty": 10.0,
            "rounded_final": 10.0,
            "captured_at": datetime.utcnow().isoformat(),
        }
        trade = Trade(
            ticker=ticker, action="BUY", quantity=10.0, price=100.0,
            strategy="cf_test", signal_source="live_engine",
            confidence=0.7, reason="cf-seed",
            paper=1, pnl=pnl, status="closed",
            instrument="stock",
            sizing_chain_json=json.dumps(chain),
        )
        s.add(trade)
        s.flush()
        prov = DecisionProvenance(
            trade_id=int(trade.id),
            event_status="submitted",
            ticker=ticker,
            decision_timestamp=datetime.utcnow() - timedelta(days=1),
            cycle_id=f"cf-int-{trade.id}",
        )
        s.add(prov)
        s.flush()
        return int(prov.id)


def _seed_blocked_provenance() -> int:
    """Provenance row whose policy_result lists a single hard blocker.
    Useful for the policy-CF endpoint test."""
    with session_scope() as s:
        policy_result = {
            "eligible": False,
            "blocking_factors": [{
                "category": "risk", "rule": "kill_switch_active",
                "severity": "hard", "reason": "operator hold",
                "evidence": {}, "sizing_penalty_pct": 0.0,
                "legacy_status": "kill_switch_active",
                "override_event_reason": True,
            }],
            "soft_penalties_total_pct": 0.0,
            "evaluated_at": datetime.utcnow().isoformat(),
            "rule_evaluations": [],
        }
        prov = DecisionProvenance(
            trade_id=None,
            event_status="kill_switch_active",
            ticker="MSFT",
            decision_timestamp=datetime.utcnow() - timedelta(days=1),
            cycle_id="cf-int-blocked",
            policy_result_json=json.dumps(policy_result),
        )
        s.add(prov)
        s.flush()
        return int(prov.id)


def _seed_council_provenance() -> int:
    """Provenance row carrying real agent_outputs + consensus JSON."""
    with session_scope() as s:
        outputs = [
            {
                "agent": "market", "role": "test", "stance": "buy",
                "confidence": 70, "weight": 1.0,
                "reasoning": "x", "reasoning_type": "contributing",
                "supporting_factors": ["spy_above"],
                "concerns": [],
                "source_categories": ["macro_liquidity"],
            },
            {
                "agent": "simulator", "role": "test", "stance": "abstain",
                "confidence": 0, "weight": 1.0,
                "reasoning": "no_signal",
                "reasoning_type": "insufficient_signal",
                "supporting_factors": [], "concerns": [],
            },
        ]
        consensus = {
            "stance": "buy", "confidence": 0.7,
            "recommendation": "execute", "size_multiplier": 0.85,
            "quorum_required": 1, "quorum_met": True,
        }
        prov = DecisionProvenance(
            trade_id=None,
            event_status="signal_only",
            ticker="NVDA",
            decision_timestamp=datetime.utcnow() - timedelta(days=1),
            cycle_id="cf-int-council",
            agent_outputs_json=json.dumps(outputs),
            consensus_json=json.dumps(consensus),
        )
        s.add(prov)
        s.flush()
        return int(prov.id)


# ── 1. GET bundle returns 200 with the expected shape ────────────────


def test_get_bundle_returns_200_with_sizing_panel():
    prov_id = _seed_closed_trade_with_provenance(pnl=250.0)
    client = TestClient(app)
    resp = client.get(f"/learning/counterfactual/{prov_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provenance_id"] == prov_id
    assert body["sizing"] is not None
    assert body["sizing"]["original_pnl"] == pytest.approx(250.0)
    pairs = {tuple(p) for p in body["sizing"]["pnl_curve"]}
    assert (1.0, 250.0) in pairs
    assert (0.5, 125.0) in pairs
    assert "notes" in body


# ── 2. POST sizing endpoint accepts factor list ──────────────────────


def test_post_sizing_endpoint_accepts_custom_factors():
    prov_id = _seed_closed_trade_with_provenance(pnl=100.0)
    client = TestClient(app)
    resp = client.post(
        f"/learning/counterfactual/{prov_id}/sizing",
        json={"factors": [0.25, 0.75]},
    )
    assert resp.status_code == 200
    body = resp.json()["counterfactual"]
    assert body["factors"] == [0.25, 0.75]
    pairs = {tuple(p) for p in body["pnl_curve"]}
    assert (0.25, 25.0) in pairs
    assert (0.75, 75.0) in pairs


# ── 3. POST policy endpoint accepts rule_name ────────────────────────


def test_post_policy_endpoint_overrides_named_blocker():
    prov_id = _seed_blocked_provenance()
    client = TestClient(app)
    resp = client.post(
        f"/learning/counterfactual/{prov_id}/policy",
        json={"rule_name": "kill_switch_active"},
    )
    assert resp.status_code == 200
    body = resp.json()["counterfactual"]
    assert body["rule_overridden"] == "kill_switch_active"
    assert body["eligible_with_override"] is True

    # Asking about a rule that didn't fire returns 404.
    resp2 = client.post(
        f"/learning/counterfactual/{prov_id}/policy",
        json={"rule_name": "low_confidence"},
    )
    assert resp2.status_code == 404


# ── 4. POST consensus endpoint accepts agent + stance + confidence ──


def test_post_consensus_endpoint_flips_one_agent():
    prov_id = _seed_council_provenance()
    client = TestClient(app)
    resp = client.post(
        f"/learning/counterfactual/{prov_id}/consensus",
        json={
            "agent": "simulator",
            "new_stance": "buy",
            "new_confidence": 70,
        },
    )
    assert resp.status_code == 200
    body = resp.json()["counterfactual"]
    assert body["agent_flipped"] == "simulator"
    assert body["new_stance"] == "buy"
    assert body["new_confidence"] == 70
    assert "stance" in body["new_consensus"]

    # Bad stance → 400.
    resp_bad = client.post(
        f"/learning/counterfactual/{prov_id}/consensus",
        json={
            "agent": "simulator",
            "new_stance": "LONG",
            "new_confidence": 70,
        },
    )
    assert resp_bad.status_code == 400


# ── 5. Cache table is populated; repeated GET returns same cache id ─


def test_bundle_cache_populated_and_returned_on_repeated_call():
    prov_id = _seed_closed_trade_with_provenance(pnl=11.0)
    client = TestClient(app)
    first = client.get(f"/learning/counterfactual/{prov_id}").json()
    second = client.get(f"/learning/counterfactual/{prov_id}").json()
    assert first.get("_cache_id") is not None
    assert second.get("_cache_id") == first.get("_cache_id")
    with session_scope() as s:
        rows = s.query(CounterfactualReplay).filter(
            CounterfactualReplay.provenance_id == prov_id,
            CounterfactualReplay.variation_kind == "bundle",
        ).all()
        assert len(rows) >= 1


# ── 6. Decision cockpit response now carries counterfactuals ─────────


def test_decision_cockpit_response_includes_counterfactuals_key():
    prov_id = _seed_closed_trade_with_provenance(pnl=42.0)
    client = TestClient(app)
    resp = client.get(f"/decision/cockpit/{prov_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "counterfactuals" in body
    cf = body["counterfactuals"]
    assert cf is not None
    assert cf["provenance_id"] == prov_id
    assert cf["sizing"] is not None
    assert cf["sizing"]["original_pnl"] == pytest.approx(42.0)
