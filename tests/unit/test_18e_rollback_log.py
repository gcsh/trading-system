"""MITS Phase 18.E — Hypothesis Studio rollback-log unit tests.

Covers the 4 ``/learning/{approve,rollback,audit-log,flags}`` endpoints
+ the LearningRollbackLog model invariants:

  * approve writes row + sets operator_approved=1 + writes audit entry
  * rollback writes row + sets operator_approved=0 + writes audit entry
  * duplicate approve is idempotent (2 audit entries; row state unchanged)
  * audit log respects table filter + limit
  * GET /learning/flags returns the 5 safety flags
  * guardrail: cannot approve a row that doesn't exist (404)
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.db import session_scope
from backend.main import app
from backend.models.agent_weight_history import AgentWeightHistory
from backend.models.learned_attribution import LearnedAttribution
from backend.models.learning_rollback_log import (
    ACTION_APPROVE,
    ACTION_ROLLBACK,
    ALLOWED_TABLES,
    LearningRollbackLog,
    TABLE_AGENT_WEIGHT_HISTORY,
    TABLE_LEARNED_ATTRIBUTION,
    TABLE_POLICY_TUNINGS,
)
from backend.models.policy_tuning import PolicyTuning


pytestmark = [pytest.mark.unit]


# ── Test fixtures ────────────────────────────────────────────────────


def _seed_attribution_row() -> int:
    """Insert one learned_attribution row + return its id."""
    with session_scope() as s:
        row = LearnedAttribution(
            computed_at=datetime.utcnow(),
            scope_kind="agent",
            scope_name="market",
            window_days=30,
            n_closed=50,
            hit_rate=0.62,
            hit_rate_wilson_lower=0.5,
            hit_rate_wilson_upper=0.73,
            mean_pnl_pct=1.5,
            brier_score=0.18,
            ece=0.04,
            payload_json=json.dumps({"_test": "18e"}),
            notes=None,
            operator_reviewed=0,
            operator_approved=0,
        )
        s.add(row)
        s.flush()
        return int(row.id)


def _seed_policy_tuning_row() -> int:
    with session_scope() as s:
        row = PolicyTuning(
            computed_at=datetime.utcnow(),
            rule_name="low_confidence_block",
            threshold_attr="confidence_min",
            current_value=0.55,
            recommended_value=0.60,
            recommendation_confidence="high",
            rationale="test seed",
            payload_json=None,
            operator_reviewed=0,
            operator_approved=0,
        )
        s.add(row)
        s.flush()
        return int(row.id)


def _seed_weight_row() -> int:
    with session_scope() as s:
        row = AgentWeightHistory(
            computed_at=datetime.utcnow(),
            agent="market",
            base_weight=1.0,
            weight_proposed=1.2,
            weight_active=1.0,
            adaptive_multiplier=1.2,
            n_closed=50,
            confidence_level="high",
            rationale="test seed",
            payload_json=None,
            operator_reviewed=0,
            operator_approved=0,
        )
        s.add(row)
        s.flush()
        return int(row.id)


# ── 1. approve writes row + flips flag + audit ───────────────────────


def test_approve_writes_row_and_audit():
    """POST /learning/approve flips operator_reviewed + operator_approved
    on the target row AND writes a single learning_rollback_log entry."""
    row_id = _seed_attribution_row()
    client = TestClient(app)

    with session_scope() as s:
        before = s.execute(
            select(func.count()).select_from(LearningRollbackLog)
            .where(LearningRollbackLog.table_name == TABLE_LEARNED_ATTRIBUTION)
            .where(LearningRollbackLog.row_id == row_id)
        ).scalar() or 0

    resp = client.post(
        "/learning/approve",
        json={
            "table": TABLE_LEARNED_ATTRIBUTION,
            "row_id": row_id,
            "notes": "looks calibrated",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["action"] == ACTION_APPROVE
    assert body["table"] == TABLE_LEARNED_ATTRIBUTION
    assert body["row_id"] == row_id

    # Target row state.
    with session_scope() as s:
        row = s.get(LearnedAttribution, row_id)
        assert row.operator_reviewed == 1
        assert row.operator_approved == 1
        after = s.execute(
            select(func.count()).select_from(LearningRollbackLog)
            .where(LearningRollbackLog.table_name == TABLE_LEARNED_ATTRIBUTION)
            .where(LearningRollbackLog.row_id == row_id)
        ).scalar() or 0
        # Audit entries grew by exactly 1.
        assert after == before + 1


# ── 2. rollback writes row + flips flag + audit ──────────────────────


def test_rollback_sets_approved_zero_and_writes_audit():
    row_id = _seed_policy_tuning_row()
    client = TestClient(app)

    # First approve so we can test the rollback flips back to 0.
    a = client.post(
        "/learning/approve",
        json={"table": TABLE_POLICY_TUNINGS, "row_id": row_id},
    )
    assert a.status_code == 200

    r = client.post(
        "/learning/rollback",
        json={
            "table": TABLE_POLICY_TUNINGS,
            "row_id": row_id,
            "notes": "false signal",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == ACTION_ROLLBACK

    with session_scope() as s:
        row = s.get(PolicyTuning, row_id)
        assert row.operator_reviewed == 1
        assert row.operator_approved == 0  # flipped back

        # 2 audit rows now: 1 approve + 1 rollback.
        audit = s.execute(
            select(LearningRollbackLog)
            .where(LearningRollbackLog.table_name == TABLE_POLICY_TUNINGS)
            .where(LearningRollbackLog.row_id == row_id)
            .order_by(LearningRollbackLog.created_at)
        ).scalars().all()
        assert len(audit) == 2
        assert audit[0].action == ACTION_APPROVE
        assert audit[1].action == ACTION_ROLLBACK


# ── 3. duplicate approve is idempotent (row), additive (audit) ───────


def test_duplicate_approve_is_idempotent_on_row():
    """Approving an already-approved row leaves the row in the same
    state (1, 1) but writes a second audit entry — the ledger always
    captures the operator's action, even when nothing visibly changed
    on the target row."""
    row_id = _seed_weight_row()
    client = TestClient(app)
    payload = {"table": TABLE_AGENT_WEIGHT_HISTORY, "row_id": row_id}

    r1 = client.post("/learning/approve", json=payload)
    r2 = client.post("/learning/approve", json=payload)
    assert r1.status_code == 200 and r2.status_code == 200

    with session_scope() as s:
        row = s.get(AgentWeightHistory, row_id)
        assert row.operator_reviewed == 1
        assert row.operator_approved == 1
        n_audit = s.execute(
            select(func.count()).select_from(LearningRollbackLog)
            .where(LearningRollbackLog.table_name == TABLE_AGENT_WEIGHT_HISTORY)
            .where(LearningRollbackLog.row_id == row_id)
        ).scalar() or 0
        assert n_audit >= 2  # at least two entries written for two calls


# ── 4. audit log respects table filter + limit ───────────────────────


def test_audit_log_filter_and_limit():
    """GET /learning/audit-log?table=X&limit=N must filter to that
    table and bound the response at N rows."""
    a_id = _seed_attribution_row()
    p_id = _seed_policy_tuning_row()
    client = TestClient(app)

    client.post(
        "/learning/approve",
        json={"table": TABLE_LEARNED_ATTRIBUTION, "row_id": a_id},
    )
    client.post(
        "/learning/rollback",
        json={"table": TABLE_POLICY_TUNINGS, "row_id": p_id},
    )

    # Filter to learned_attribution.
    r = client.get(
        f"/learning/audit-log?table={TABLE_LEARNED_ATTRIBUTION}&limit=50",
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["table"] == TABLE_LEARNED_ATTRIBUTION
    for row in body["rows"]:
        assert row["table_name"] == TABLE_LEARNED_ATTRIBUTION

    # Limit honored.
    r2 = client.get("/learning/audit-log?limit=1")
    assert r2.status_code == 200
    assert len(r2.json()["rows"]) <= 1


# ── 5. GET /learning/flags returns the 5 keys ────────────────────────


def test_get_flags_returns_five_safety_keys():
    """Flags endpoint always returns the 5 documented safety flags as
    booleans; default OFF unless the operator flipped env vars."""
    client = TestClient(app)
    r = client.get("/learning/flags")
    assert r.status_code == 200
    body = r.json()
    expected = {
        "decision_rollback_enabled",
        "policy_tuning_advisory_enabled",
        "policy_tuning_auto_apply_enabled",
        "adaptive_weights_advisory_enabled",
        "adaptive_weights_apply_enabled",
    }
    assert set(body.keys()) == expected
    for k in expected:
        assert isinstance(body[k], bool)


# ── 6. guardrail: cannot approve a row that doesn't exist ────────────


def test_approve_missing_row_returns_404():
    """row_id pointing at no row in the target table → 404 with a
    detail mentioning the missing id. Audit ledger is NOT written."""
    client = TestClient(app)
    with session_scope() as s:
        before = s.execute(
            select(func.count()).select_from(LearningRollbackLog)
        ).scalar() or 0
    r = client.post(
        "/learning/approve",
        json={
            "table": TABLE_LEARNED_ATTRIBUTION,
            "row_id": 999_999_999,
        },
    )
    assert r.status_code == 404
    assert "999999999" in str(r.json().get("detail", ""))
    with session_scope() as s:
        after = s.execute(
            select(func.count()).select_from(LearningRollbackLog)
        ).scalar() or 0
    assert after == before


# ── Extra defensive coverage — bad input shapes ─────────────────────


def test_approve_with_unknown_table_returns_400():
    """Unknown table value rejected before any DB action."""
    client = TestClient(app)
    r = client.post(
        "/learning/approve",
        json={"table": "not_a_real_table", "row_id": 1},
    )
    assert r.status_code == 400
    assert "learning table" in str(r.json().get("detail", "")).lower()


def test_approve_without_row_id_returns_400():
    """Missing row_id rejected with the standard 400 detail."""
    client = TestClient(app)
    r = client.post(
        "/learning/approve",
        json={"table": TABLE_LEARNED_ATTRIBUTION},
    )
    assert r.status_code == 400
    assert "row_id" in str(r.json().get("detail", ""))


def test_allowed_tables_align_with_models():
    """Sanity invariant: the ALLOWED_TABLES tuple must mention exactly
    the 3 learning tables 18.E governs — if a new learning surface is
    added later, this test forces the engineer to extend the mapping."""
    assert set(ALLOWED_TABLES) == {
        TABLE_LEARNED_ATTRIBUTION,
        TABLE_POLICY_TUNINGS,
        TABLE_AGENT_WEIGHT_HISTORY,
    }
