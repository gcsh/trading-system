"""MITS Phase 18.A — /learning/attribution* endpoint integration tests.

Drives the four GET endpoints + the POST recompute against a freshly
seeded DB so each test runs in isolation. The Step 5 brief asks for
four cases:

  1. GET /learning/attribution/agents returns valid JSON listing every
     known agent (with ``insufficient_sample_size`` flags on empty seed).
  2. POST /learning/attribution/recompute persists rows + returns 200.
  3. learned_attribution table accepts the new rows.
  4. Back-compat: existing decision_provenance + trade rows are
     UNMODIFIED by the new endpoints.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.db import session_scope
from backend.main import app
from backend.models.decision_provenance import DecisionProvenance
from backend.models.learned_attribution import LearnedAttribution
from backend.models.trade import Trade
from backend.bot.learning.attribution import KNOWN_AGENTS, KNOWN_AXES


pytestmark = [pytest.mark.integration]


def _seed_closed_decision_with_agent_outputs(
    *, ticker: str = "AAPL", strategy: str = "momentum_long",
    pnl: float = 25.0, agent_confidence_pct: int = 70,
    decision_days_ago: int = 5,
) -> int:
    """Seed one closed Trade + linked DecisionProvenance row with all
    8 known agents voting BUY. Returns the Trade.id for cleanup checks."""
    with session_scope() as s:
        trade = Trade(
            ticker=ticker,
            action="BUY",
            quantity=10,
            price=100.0,
            strategy=strategy,
            signal_source="live_engine",
            confidence=0.7,
            reason="seed",
            paper=1,
            pnl=pnl,
            status="closed",
            instrument="stock",
        )
        s.add(trade)
        s.flush()
        trade_id = int(trade.id)

        agent_outputs = [
            {
                "agent": name,
                "role": "test",
                "stance": "buy",
                "confidence": agent_confidence_pct,
                "weight": 1.0,
                "reasoning": "seed",
                "reasoning_type": "contributing",
                "supporting_factors": [],
                "concerns": [],
            }
            for name in KNOWN_AGENTS
        ]
        # ConfidenceBreakdown shape — 15.D stores [0, 1] per axis.
        cb = {axis: 0.75 for axis in KNOWN_AXES}
        consensus = {
            "stance": "buy", "confidence": 0.7,
            "recommendation": "execute",
            "confidence_breakdown": cb,
        }
        prov = DecisionProvenance(
            trade_id=trade_id,
            event_status="submitted",
            ticker=ticker,
            decision_timestamp=datetime.utcnow() - timedelta(
                days=decision_days_ago
            ),
            cycle_id=f"seed-{trade_id}",
            consensus_json=json.dumps(consensus),
            agent_outputs_json=json.dumps(agent_outputs),
            regime_vector_json=json.dumps({"trend": "trending_up"}),
        )
        s.add(prov)
        s.flush()
        return trade_id


# ── 1. GET /learning/attribution/agents lists every known agent ──────


def test_get_attribution_agents_lists_all_known_agents_even_when_empty(
    temp_db,
):
    """Empty DB — endpoint MUST still return one entry per known agent
    with ``insufficient_sample_size`` flags. Operator never sees an
    empty page; they see "not enough data yet" for each agent."""
    # Trigger a fresh compute so the latest_attribution_rows query has
    # rows to return.
    with TestClient(app) as client:
        recompute = client.post(
            "/learning/attribution/recompute?window=90&persist=true",
        )
        assert recompute.status_code == 200, recompute.text
        assert recompute.json()["ok"] is True

        r = client.get("/learning/attribution/agents?window=90")
        assert r.status_code == 200
        body = r.json()
        assert body["window_days"] == 90
        agent_names = {a["scope_name"] for a in body["agents"]}
        assert agent_names == set(KNOWN_AGENTS)
        # Empty corpus ⇒ every agent must be insufficient_sample_size.
        for entry in body["agents"]:
            assert entry["n_closed"] == 0
            assert entry["hit_rate"] is None
            assert entry["notes"] is not None
            assert "insufficient_sample_size" in entry["notes"]


# ── 2. POST recompute persists rows + returns ok ─────────────────────


def test_post_recompute_creates_learned_attribution_rows(temp_db):
    with TestClient(app) as client:
        r = client.post("/learning/attribution/recompute?window=90&persist=true")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["persisted"] is True
        # 8 agents + 6 axes + 0 strategies (no trades seeded)
        assert body["counts"]["agent"] == len(KNOWN_AGENTS)
        assert body["counts"]["axis"] == len(KNOWN_AXES)
        assert body["counts"]["strategy"] == 0

    # Confirm the rows are persisted at the DB layer.
    with session_scope() as s:
        rows = s.query(LearnedAttribution).all()
        assert len(rows) == len(KNOWN_AGENTS) + len(KNOWN_AXES)


# ── 3. learned_attribution table accepts rows + payload round-trips ──


def test_learned_attribution_table_accepts_full_payload(temp_db):
    """Round-trip: seed a sample, compute, then read back. The
    payload_json column must round-trip the full dataclass projection
    so the API consumer sees richer detail than the flat numeric columns."""
    with TestClient(app) as client:
        client.post("/learning/attribution/recompute?window=90&persist=true")
        r = client.get("/learning/attribution/axes?window=90")
        assert r.status_code == 200
        body = r.json()
        # Every axis appears with a payload + numeric n_closed=0 (empty seed).
        axis_names = {a["scope_name"] for a in body["axes"]}
        assert axis_names == set(KNOWN_AXES)
        for entry in body["axes"]:
            assert entry["payload"] is not None
            assert entry["payload"]["axis"] == entry["scope_name"]
            assert entry["payload"]["n_closed"] == 0


# ── 4. Back-compat: existing DP + trades untouched ───────────────────


def test_recompute_does_not_mutate_existing_provenance_or_trades(temp_db):
    """The attribution pipeline is READ-ONLY against the source tables.
    Seed one row in each; recompute; reread; assert byte-for-byte
    equality on the original rows."""
    trade_id = _seed_closed_decision_with_agent_outputs(
        ticker="AAPL", strategy="momentum_long", pnl=25.0,
        agent_confidence_pct=70, decision_days_ago=5,
    )
    # Snapshot the trade + prov before recompute.
    with session_scope() as s:
        before_trade = s.query(Trade).filter(Trade.id == trade_id).first()
        assert before_trade is not None
        before_trade_dict = before_trade.to_dict()
        before_prov = s.query(DecisionProvenance).filter(
            DecisionProvenance.trade_id == trade_id
        ).first()
        assert before_prov is not None
        before_prov_dict = before_prov.to_dict()

    with TestClient(app) as client:
        r = client.post(
            "/learning/attribution/recompute?window=90&persist=true"
        )
        assert r.status_code == 200

    with session_scope() as s:
        after_trade = s.query(Trade).filter(Trade.id == trade_id).first()
        after_prov = s.query(DecisionProvenance).filter(
            DecisionProvenance.trade_id == trade_id
        ).first()
        assert after_trade is not None
        assert after_prov is not None
        assert after_trade.to_dict() == before_trade_dict
        assert after_prov.to_dict() == before_prov_dict

    # Now confirm the seed row got counted ONCE — the n_closed for each
    # known agent should be 1 in the recomputed report (even though the
    # min_n guardrail then suppresses every metric).
    with TestClient(app) as client:
        r = client.get("/learning/attribution/agents?window=90")
        body = r.json()
        for entry in body["agents"]:
            assert entry["n_closed"] == 1
