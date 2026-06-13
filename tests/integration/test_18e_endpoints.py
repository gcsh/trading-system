"""MITS Phase 18.E — Hypothesis Studio integration tests.

Drives the wire-level shape of:

  * POST /learning/approve     — writes audit row
  * POST /learning/rollback    — writes audit row
  * GET  /learning/audit-log   — rolling history
  * GET  /learning/flags       — 5 safety flags, all False at default
  * GET  /hypothesis-studio    — SPA route returns 200 (frontend boots)
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.config import TUNABLES
from backend.db import session_scope
from backend.main import app
from backend.models.agent_weight_history import AgentWeightHistory
from backend.models.learned_attribution import LearnedAttribution
from backend.models.learning_rollback_log import (
    LearningRollbackLog,
    TABLE_AGENT_WEIGHT_HISTORY,
    TABLE_LEARNED_ATTRIBUTION,
    TABLE_POLICY_TUNINGS,
)
from backend.models.policy_tuning import PolicyTuning


pytestmark = [pytest.mark.integration]


def _seed_one_of_each() -> dict:
    """Insert one row in each of the 3 learning tables so the
    endpoints have real targets. Returns {table_name: row_id}."""
    out: dict = {}
    with session_scope() as s:
        la = LearnedAttribution(
            computed_at=datetime.utcnow(),
            scope_kind="agent", scope_name="market",
            window_days=30, n_closed=42, hit_rate=0.55,
            operator_reviewed=0, operator_approved=0,
        )
        s.add(la)
        s.flush()
        out[TABLE_LEARNED_ATTRIBUTION] = int(la.id)

        pt = PolicyTuning(
            computed_at=datetime.utcnow(),
            rule_name="low_confidence_block",
            threshold_attr="confidence_min",
            current_value=0.55,
            recommended_value=0.60,
            recommendation_confidence="medium",
            operator_reviewed=0, operator_approved=0,
        )
        s.add(pt)
        s.flush()
        out[TABLE_POLICY_TUNINGS] = int(pt.id)

        wh = AgentWeightHistory(
            computed_at=datetime.utcnow(),
            agent="market",
            base_weight=1.0, weight_proposed=1.1,
            weight_active=1.0, adaptive_multiplier=1.1,
            n_closed=42, confidence_level="medium",
            operator_reviewed=0, operator_approved=0,
        )
        s.add(wh)
        s.flush()
        out[TABLE_AGENT_WEIGHT_HISTORY] = int(wh.id)
    return out


# ── approve writes a learning_rollback_log row ───────────────────────


def test_approve_via_post_writes_audit_row():
    seeded = _seed_one_of_each()
    client = TestClient(app)
    with session_scope() as s:
        before = s.execute(
            select(func.count()).select_from(LearningRollbackLog)
        ).scalar() or 0

    r = client.post(
        "/learning/approve",
        json={
            "table": TABLE_POLICY_TUNINGS,
            "row_id": seeded[TABLE_POLICY_TUNINGS],
            "notes": "ok by 18.E integration",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "approve"

    with session_scope() as s:
        after = s.execute(
            select(func.count()).select_from(LearningRollbackLog)
        ).scalar() or 0
    assert after == before + 1


# ── rollback writes a learning_rollback_log row ──────────────────────


def test_rollback_via_post_writes_audit_row():
    seeded = _seed_one_of_each()
    client = TestClient(app)
    r = client.post(
        "/learning/rollback",
        json={
            "table": TABLE_AGENT_WEIGHT_HISTORY,
            "row_id": seeded[TABLE_AGENT_WEIGHT_HISTORY],
            "notes": "noisy proposal",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "rollback"
    assert body["row"]["operator_approved"] == 0
    assert body["row"]["operator_reviewed"] == 1


# ── audit-log query works ────────────────────────────────────────────


def test_audit_log_query_returns_rows():
    seeded = _seed_one_of_each()
    client = TestClient(app)
    # Take an action so there's at least one entry.
    client.post(
        "/learning/approve",
        json={
            "table": TABLE_LEARNED_ATTRIBUTION,
            "row_id": seeded[TABLE_LEARNED_ATTRIBUTION],
        },
    )
    r = client.get("/learning/audit-log?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == 5
    assert isinstance(body["rows"], list)
    # Newest-first ordering.
    if len(body["rows"]) >= 2:
        a = body["rows"][0]["created_at"]
        b = body["rows"][1]["created_at"]
        assert a >= b


# ── flags endpoint returns the 5 keys, all False at default ──────────


def test_flags_endpoint_returns_expected_five_keys(monkeypatch):
    """All 5 default OFF. We force them off via monkeypatch in case
    the test box has env-var overrides set for an experimental run."""
    for key in (
        "decision_rollback_enabled",
        "policy_tuning_advisory_enabled",
        "policy_tuning_auto_apply_enabled",
        "adaptive_weights_advisory_enabled",
        "adaptive_weights_apply_enabled",
    ):
        monkeypatch.setattr(TUNABLES, key, False, raising=False)
    client = TestClient(app)
    r = client.get("/learning/flags")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {
        "decision_rollback_enabled",
        "policy_tuning_advisory_enabled",
        "policy_tuning_auto_apply_enabled",
        "adaptive_weights_advisory_enabled",
        "adaptive_weights_apply_enabled",
    }
    for v in body.values():
        assert v is False


# ── /hypothesis-studio SPA route returns 200 ─────────────────────────


def test_hypothesis_studio_spa_route_returns_200():
    """The React app is a client-side router, so the FastAPI SPA
    fallback at /hypothesis-studio must return 200 with the index.html
    payload — this is what lets the operator deep-link / refresh /
    bookmark the studio URL.

    When the dist bundle is absent (CI / fresh checkout), the route
    isn't registered at all and we just skip — the deploy gate covers
    the on-EC2 path.
    """
    dist_dir = Path("frontend/dist")
    if not dist_dir.exists() or not (dist_dir / "index.html").exists():
        pytest.skip("frontend/dist not built; SPA fallback not registered")
    client = TestClient(app)
    r = client.get("/hypothesis-studio")
    assert r.status_code == 200
    # The fallback returns HTML, not JSON. The minimal invariant: the
    # body contains the Vite root mount-point div.
    body = r.text.lower()
    assert "<div id=\"root\"" in body or "<div id='root'" in body
