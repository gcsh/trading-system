"""MITS Phase 18-FU Stream A — /learning/funnel* endpoint integration tests.

Drives the three new endpoints + the cockpit funnel_snapshot key
against a freshly seeded DB. The Step 6 brief asks for four tests;
this file ships 5 (adds a regression-protection test for the
top_surgical_change_candidate auto_apply=False contract).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.db import session_scope
from backend.main import app
from backend.models.decision_funnel_daily import DecisionFunnelDaily
from backend.models.decision_provenance import DecisionProvenance


pytestmark = [pytest.mark.integration]


def _seed_provenance_rows(*, n: int = 5) -> None:
    """Drop a handful of cheap provenance rows in the window so the
    funnel compute has something to count. We don't need closed Trades
    — every stage degrades gracefully on missing data."""
    with session_scope() as s:
        for i in range(n):
            policy = {
                "eligible": False,
                "blocking_factors": [
                    {
                        "rule": "signal_hold", "category": "strategy",
                        "severity": "hard", "reason": "HOLD",
                        "evidence": {}, "legacy_status": "hold",
                    },
                    {
                        "rule": "low_confidence", "category": "strategy",
                        "severity": "hard", "reason": "low conf",
                        "evidence": {}, "legacy_status": "low_confidence",
                    },
                ],
                "soft_penalties_total_pct": 0.0,
            }
            consensus = {
                "stance": "hold", "confidence": 0.4,
                "recommendation": "abstain",
                "quorum_met": False, "quorum_required": 3,
                "contributing_count": 1,
            }
            prov = DecisionProvenance(
                trade_id=None,
                event_status="hold",
                ticker=f"T{i}",
                decision_timestamp=datetime.utcnow() - timedelta(hours=i),
                cycle_id=f"funnel-seed-{i}",
                policy_result_json=json.dumps(policy),
                consensus_json=json.dumps(consensus),
                agent_outputs_json=json.dumps([]),
            )
            s.add(prov)


# ── 1. GET /learning/funnel returns 200 ──────────────────────────────


def test_get_learning_funnel_returns_200(temp_db):
    _seed_provenance_rows(n=3)
    with TestClient(app) as client:
        r = client.get("/learning/funnel?window=14&recompute=true")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] == "on_demand"
        assert body["window_days"] == 14
        assert body["persisted"] is False
        report = body["report"]
        assert isinstance(report["stages"], list)
        assert len(report["stages"]) == 10
        assert report["top_surgical_change_candidate"]["auto_apply"] is False


# ── 2. POST recompute writes a decision_funnel_daily row ─────────────


def test_post_funnel_recompute_writes_row(temp_db):
    _seed_provenance_rows(n=4)
    with TestClient(app) as client:
        r = client.post("/learning/funnel/recompute?window=14")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["persisted"] is True
        assert body["meta"]["row_id"] > 0
        assert body["top_surgical_change_candidate"]["auto_apply"] is False
    with session_scope() as s:
        rows = s.query(DecisionFunnelDaily).all()
        n_rows = len(rows)
        n_evals = int(rows[0].n_evaluations) if rows else 0
    assert n_rows == 1
    assert n_evals >= 4


# ── 3. GET history returns rolling rows ──────────────────────────────


def test_get_funnel_history_returns_rolling_rows(temp_db):
    _seed_provenance_rows(n=3)
    today = datetime.utcnow().date()
    with TestClient(app) as client:
        # Persist three days' worth of rows.
        for delta in (2, 1, 0):
            r = client.post(
                f"/learning/funnel/recompute?window=1"
                f"&target_date={(today - timedelta(days=delta)).isoformat()}",
            )
            assert r.status_code == 200, r.text
        r = client.get("/learning/funnel/history?days=10")
        assert r.status_code == 200
        body = r.json()
        assert body["days"] == 10
        assert body["count"] == 3
        # Newest first.
        dates = [row["date"] for row in body["rows"]]
        assert dates == sorted(dates, reverse=True)


# ── 4. Cockpit funnel_snapshot key present ───────────────────────────


def test_cockpit_funnel_snapshot_key_present(temp_db):
    """Plant a provenance row + a funnel_daily row, then hit the
    /decision/cockpit/{identifier} endpoint and assert
    learning_insights.funnel_snapshot is populated."""
    _seed_provenance_rows(n=2)
    with TestClient(app) as client:
        # Persist a funnel row so the cockpit has something to surface.
        client.post("/learning/funnel/recompute?window=14")
        # Use the seeded ticker T0; cockpit resolves by ticker.
        r = client.get("/decision/cockpit/T0")
        assert r.status_code == 200, r.text
        body = r.json()
        learning = body.get("learning_insights")
        assert learning is not None, "learning_insights must be present"
        assert "funnel_snapshot" in learning, (
            "funnel_snapshot key must be under learning_insights"
        )
        snap = learning["funnel_snapshot"]
        assert snap is not None
        # If a row was written, it carries n_evaluations.
        assert "n_evaluations" in snap
        # The surgical advisory must respect the auto_apply=False
        # contract regardless of branch.
        if snap.get("top_surgical_change_candidate"):
            assert (
                snap["top_surgical_change_candidate"]["auto_apply"]
                is False
            )


# ── 5. Surgical advisory is NEVER auto-applied (regression guard) ────


def test_surgical_advisory_auto_apply_always_false(temp_db):
    """The Stream A contract: every surgical change candidate is
    advisory. Any future code that flips auto_apply=True must trip
    this test."""
    _seed_provenance_rows(n=5)
    with TestClient(app) as client:
        r1 = client.get("/learning/funnel?recompute=true")
        assert r1.status_code == 200
        body1 = r1.json()
        adv1 = body1["report"]["top_surgical_change_candidate"]
        assert adv1["auto_apply"] is False

        r2 = client.post("/learning/funnel/recompute?window=14")
        assert r2.status_code == 200
        adv2 = r2.json()["top_surgical_change_candidate"]
        assert adv2["auto_apply"] is False
