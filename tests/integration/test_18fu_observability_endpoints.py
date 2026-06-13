"""MITS Phase 18-FU Stream D — observability endpoint integration tests.

Drives:

  * GET  /learning/observability/weight-applications
  * GET  /learning/observability/impact
  * POST /learning/observability/impact/recompute
  * GET  /learning/observability/health

All flags default OFF; the cockpit must render gracefully on empty
data so the endpoints return 200 + empty rows lists / zero counts.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.bot.learning.counterfactual import COUNTERFACTUAL_CODE_VERSION
from backend.bot.learning.impact_measurement import (
    EVENT_TYPE_WEIGHT_APPLY,
    LearningImpactReport,
    WindowMetrics,
    persist_impact_reports,
)
from backend.config import TUNABLES
from backend.main import app
from backend.models.weight_application_log import WeightApplicationLog


pytestmark = [pytest.mark.integration]


def _session():
    from backend.db import session_scope
    return session_scope()


def test_get_weight_applications_returns_200_with_empty_rows_by_default():
    """With apply_enabled OFF (default) the log table is empty + the
    endpoint returns 200 with rows=[] + the flag state."""
    client = TestClient(app)
    response = client.get("/learning/observability/weight-applications")
    assert response.status_code == 200
    body = response.json()
    assert "rows" in body
    assert isinstance(body["rows"], list)
    assert body["n_rows"] == len(body["rows"])
    assert body["apply_enabled"] in (False, True)
    assert body["advisory_enabled"] in (False, True)
    assert body["ttl_days"] > 0


def test_get_weight_applications_returns_seeded_row(temp_db):
    """Seed one log row + the endpoint returns it with decoded
    weight_set."""
    client = TestClient(app)
    with _session() as s:
        s.add(WeightApplicationLog(
            applied_at=datetime.utcnow(),
            cycle_id="cycle-int-1",
            weight_set_json=json.dumps({"market": 1.1, "macro": 0.95}),
            composite_quality_at_apply=70.0,
        ))
        s.flush()
    response = client.get(
        "/learning/observability/weight-applications?limit=5",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["n_rows"] >= 1
    row = body["rows"][0]
    assert row["cycle_id"] == "cycle-int-1"
    assert row["weight_set"] == {"market": 1.1, "macro": 0.95}
    assert row["composite_quality_at_apply"] == 70.0


def test_get_impact_returns_200_with_filter():
    client = TestClient(app)
    # Unfiltered.
    r1 = client.get("/learning/observability/impact?limit=5")
    assert r1.status_code == 200
    body1 = r1.json()
    assert "rows" in body1
    assert "min_n_for_significance" in body1
    # Bad event_type.
    r2 = client.get(
        "/learning/observability/impact?event_type=does_not_exist",
    )
    assert r2.status_code == 400
    # Good event_type.
    r3 = client.get(
        "/learning/observability/impact"
        f"?event_type={EVENT_TYPE_WEIGHT_APPLY}",
    )
    assert r3.status_code == 200


def test_get_impact_returns_seeded_row(temp_db):
    """Persist one impact report + the endpoint returns it."""
    client = TestClient(app)
    rpt = LearningImpactReport(
        learning_event_type=EVENT_TYPE_WEIGHT_APPLY,
        event_id=101,
        event_timestamp=datetime.utcnow() - timedelta(days=1),
        metrics_before=WindowMetrics(
            n_decisions=5, n_closed=2,
            submission_rate=0.4, composite_mean=55.0,
            hit_rate=None, mean_pnl_pct=None,
        ),
        metrics_after=WindowMetrics(
            n_decisions=4, n_closed=2,
            submission_rate=0.5, composite_mean=60.0,
            hit_rate=None, mean_pnl_pct=None,
        ),
        delta={"submission_rate": 0.1, "composite_mean": 5.0,
               "hit_rate": None, "mean_pnl_pct": None},
        is_significant=False,
        note="insufficient_sample_size_for_significance",
    )
    persist_impact_reports([rpt])
    response = client.get(
        f"/learning/observability/impact?"
        f"event_type={EVENT_TYPE_WEIGHT_APPLY}&limit=5",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["n_rows"] >= 1
    row = body["rows"][0]
    assert row["learning_event_type"] == EVENT_TYPE_WEIGHT_APPLY
    assert row["event_id"] == 101
    assert row["is_significant"] == 0


def test_post_impact_recompute_with_no_events_writes_zero_rows(temp_db):
    """With no learning events in the lookback window the recompute
    endpoint returns ok=True + n_events_seen=0 + written=0 (empty state
    is honest, not an error)."""
    client = TestClient(app)
    response = client.post(
        "/learning/observability/impact/recompute",
        json={"days_back": 7, "before_window_days": 3,
              "after_window_days": 3},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["n_events_seen"] == 0
    assert body["written"] == 0
    assert body["days_back"] == 7


def test_post_impact_recompute_rejects_invalid_inputs():
    client = TestClient(app)
    # days_back out of range.
    r1 = client.post(
        "/learning/observability/impact/recompute",
        json={"days_back": 0},
    )
    assert r1.status_code == 400
    # Bad before window.
    r2 = client.post(
        "/learning/observability/impact/recompute",
        json={"days_back": 7, "before_window_days": -5},
    )
    assert r2.status_code == 400
    # Garbage type.
    r3 = client.post(
        "/learning/observability/impact/recompute",
        json={"days_back": "garbage"},
    )
    assert r3.status_code == 400


def test_get_health_returns_full_shape():
    client = TestClient(app)
    response = client.get("/learning/observability/health")
    assert response.status_code == 200
    body = response.json()
    for k in (
        "gap6_weight_applications",
        "gap9_policy_tuning_stability",
        "gap10_learning_impact",
        "gap12_counterfactual_cache",
        "computed_at",
    ):
        assert k in body
    # Gap 12 must report the current code version constant.
    assert (
        body["gap12_counterfactual_cache"]["current_code_version"]
        == COUNTERFACTUAL_CODE_VERSION
    )
    # Row counts are non-negative integers.
    assert body["gap6_weight_applications"]["row_count"] >= 0
    assert body["gap9_policy_tuning_stability"]["row_count"] >= 0
    assert body["gap10_learning_impact"]["row_count"] >= 0
    assert body["gap12_counterfactual_cache"]["cache_rows"] >= 0


def test_post_weight_applications_prune_returns_ok_with_zero(temp_db):
    """With no rows in the table the prune returns ok=True + deleted=0."""
    client = TestClient(app)
    response = client.post(
        "/learning/observability/weight-applications/prune",
        json={"ttl_days": 30},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["deleted"] == 0
    assert body["ttl_days"] == 30


def test_post_weight_applications_prune_rejects_invalid_ttl():
    client = TestClient(app)
    r1 = client.post(
        "/learning/observability/weight-applications/prune",
        json={"ttl_days": 0},
    )
    assert r1.status_code == 400
    r2 = client.post(
        "/learning/observability/weight-applications/prune",
        json={"ttl_days": "garbage"},
    )
    assert r2.status_code == 400
