"""MITS Phase 18-FU Stream C — UX backend regression tests.

This test module locks the wire-level shape of the four endpoints the
new Hypothesis Studio modal + Decision Cockpit panels depend on:

  * GET  /policy/rules                                    — modal dropdown source
  * GET  /agents/list                                     — modal dropdown source
  * POST /learning/counterfactual/{id}/policy             — modal submit target
  * POST /learning/counterfactual/{id}/consensus          — modal submit target
  * POST /learning/approve                                — Gap 5 queue path
                                                            (sanity check)

These tests are intentionally read-only against the backend logic — they
exercise the public HTTP surface only, never patch business code. The
frontend changes (HypothesisStudio.jsx, DecisionCockpit.jsx,
WhatIfModal.jsx, ExecutionPanel.jsx) are verified separately via the
browser smoke gates in the deploy step.

Safety: every test honors the 5-flag OFF default; none of the tests can
flip an apply flag because they never write to TUNABLES.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Any, List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.db import session_scope
from backend.main import app
from backend.models.learned_attribution import LearnedAttribution
from backend.models.learning_rollback_log import (
    LearningRollbackLog,
    TABLE_LEARNED_ATTRIBUTION,
)


pytestmark = [pytest.mark.integration]


# ── Gap 8 dropdown sources ───────────────────────────────────────────


def test_policy_rules_endpoint_returns_registered_rules() -> None:
    """The What-If modal's Policy variant populates its rule_name
    dropdown from GET /policy/rules. The endpoint must return a non-empty
    list of dicts each carrying ``name``, ``category``, ``severity``,
    ``enabled`` so the dropdown can show "name (category/severity)".

    The exact count drifts as rules are added/removed across phases —
    we lock the schema invariant, not the magic 30. The Gap 8 spec
    quoted 30 as today's observed count; we assert "at least 8" to
    catch a regression where the registry empties out (which would
    silently leave the modal with an empty dropdown).
    """
    client = TestClient(app)
    r = client.get("/policy/rules")
    assert r.status_code == 200, r.text
    body = r.json()
    rules: List[Dict[str, Any]]
    if isinstance(body, list):
        rules = body
    else:
        rules = body.get("rules") or []
    assert isinstance(rules, list)
    assert len(rules) >= 8, (
        f"expected at least 8 registered policy rules; got {len(rules)} — "
        "the What-If modal Policy dropdown will be unusable"
    )
    for rule in rules:
        assert "name" in rule and isinstance(rule["name"], str) and rule["name"]
        assert "category" in rule
        assert "severity" in rule
        assert "enabled" in rule


def test_agents_list_endpoint_returns_known_council_agents() -> None:
    """The What-If modal's Consensus variant populates its agent
    dropdown from GET /agents/list. Must return ``{agents: [...]}`` with
    each entry shaped ``{agent: str, role: str}`` so the dropdown can
    show "agent (role)".
    """
    client = TestClient(app)
    r = client.get("/agents/list")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "agents" in body, "expected /agents/list to return {agents: [...]}"
    agents = body["agents"]
    assert isinstance(agents, list)
    assert len(agents) >= 4, (
        f"expected at least 4 council agents; got {len(agents)} — "
        "the What-If modal Consensus dropdown will be unusable"
    )
    for ag in agents:
        assert "agent" in ag
        assert "role" in ag


# ── Gap 8 modal submit paths ─────────────────────────────────────────


def _seed_decision_provenance_with_policy_block() -> int:
    """Insert a minimal decision_provenance row that carries enough
    structure for policy_counterfactual to even consider executing.

    We don't need a "valid" block — we just need a row id we can POST
    against. The endpoint returns 404 if no policy_result was persisted
    on the row, which is the path we exercise for the "rule didn't
    block" case in the test below.
    """
    from backend.models.decision_provenance import DecisionProvenance
    with session_scope() as s:
        row = DecisionProvenance(
            ticker="AAPL",
            event_status="abstained",
            decision_timestamp=datetime.utcnow(),
            policy_result_json=None,
            chairman_memo_json=None,
            consensus_json=None,
        )
        s.add(row)
        s.flush()
        return int(row.id)


def test_policy_counterfactual_validates_rule_name() -> None:
    """The modal POSTs ``{rule_name}`` to .../policy. Missing rule_name
    must come back 400 so the modal can surface a sensible error rather
    than a generic 500. We exercise the validation path here; the
    "rule actually blocked" path is exercised in 18.B's own suite.
    """
    client = TestClient(app)
    prov_id = _seed_decision_provenance_with_policy_block()
    # Empty body -> 400 because rule_name is required.
    r = client.post(
        f"/learning/counterfactual/{prov_id}/policy",
        json={},
    )
    assert r.status_code == 400, r.text
    # The decision_provenance row has no policy_result block, so the
    # endpoint returns 404 (rule didn't block) for a syntactically valid
    # rule_name. That 404 is the exact path the modal must handle.
    r2 = client.post(
        f"/learning/counterfactual/{prov_id}/policy",
        json={"rule_name": "some_rule_name"},
    )
    assert r2.status_code in (200, 404), r2.text


def test_consensus_counterfactual_validates_stance() -> None:
    """The modal POSTs ``{agent, new_stance, new_confidence}`` to
    .../consensus. We must reject an unknown stance with 400 so the
    modal's "buy/sell/hold/abstain" dropdown is the authoritative
    contract.
    """
    client = TestClient(app)
    prov_id = _seed_decision_provenance_with_policy_block()
    r = client.post(
        f"/learning/counterfactual/{prov_id}/consensus",
        json={"agent": "market", "new_stance": "MAYBE", "new_confidence": 50},
    )
    assert r.status_code == 400, r.text
    body = r.json()
    detail = str(body.get("detail", ""))
    assert "new_stance" in detail
    # A valid stance gets past validation; the actual outcome (200 or
    # 404 depending on whether agent_outputs were persisted) is fine.
    r2 = client.post(
        f"/learning/counterfactual/{prov_id}/consensus",
        json={"agent": "market", "new_stance": "buy", "new_confidence": 50},
    )
    assert r2.status_code in (200, 404), r2.text


# ── Gap 5 approve queue path sanity check ───────────────────────────


def test_approve_sets_operator_approved_and_writes_audit_row() -> None:
    """Gap 5 — the cockpit's queued/active badge depends on
    ``operator_approved=1`` being persisted by POST /learning/approve.
    This is the existing 18.E behavior; we lock it here so the UX
    rework doesn't silently lose the side-effect.

    The 5 safety flags stay OFF — we only verify the row-level write
    and the audit-log append, not any apply behavior.
    """
    # Seed one learned_attribution row.
    with session_scope() as s:
        attr = LearnedAttribution(
            computed_at=datetime.utcnow(),
            scope_kind="agent", scope_name="market",
            window_days=30, n_closed=42, hit_rate=0.55,
            operator_reviewed=0, operator_approved=0,
        )
        s.add(attr)
        s.flush()
        attr_id = int(attr.id)

    with session_scope() as s:
        before = s.execute(
            select(func.count()).select_from(LearningRollbackLog)
        ).scalar() or 0

    client = TestClient(app)
    r = client.post(
        "/learning/approve",
        json={
            "table": TABLE_LEARNED_ATTRIBUTION,
            "row_id": attr_id,
            "notes": "18-FU stream C UX gap 5 — queued path",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "approve"
    # The row snapshot in the response reflects the flag flip; the UI
    # reads this to drive the queued/active badge.
    assert body["row"]["operator_reviewed"] == 1
    assert body["row"]["operator_approved"] == 1

    with session_scope() as s:
        after = s.execute(
            select(func.count()).select_from(LearningRollbackLog)
        ).scalar() or 0
    assert after == before + 1, (
        "approve must write exactly one learning_rollback_log row — "
        "without it the cockpit audit ribbon misses the action"
    )

    # Sanity: a second approve on the same row is idempotent (no error)
    # and writes another audit row so the operator's full intent
    # history is preserved.
    r2 = client.post(
        "/learning/approve",
        json={
            "table": TABLE_LEARNED_ATTRIBUTION,
            "row_id": attr_id,
        },
    )
    assert r2.status_code == 200, r2.text
