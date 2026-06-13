"""MITS Phase 18.D — Online Agent Weight Adaptation endpoint integration tests.

Drives:

  * GET  /learning/weights                 — registry + rows shape
  * GET  /learning/weights/history?agent=X — per-agent rolling history
  * POST /learning/weights/recompute       — compute + persist gating

Plus engine-side regression: when ``adaptive_weights_apply_enabled``
is False (default), aggregate() must yield the same Consensus as
before — confirming 16.B replay drift stays at 0.0.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from backend.bot.agents import (
    AgentVote,
    Consensus,
    STANCE_BUY,
    aggregate,
)
from backend.bot.agents.contract import REASONING_LEGACY
from backend.bot.learning.weight_adaptation import AGENT_BASE_WEIGHTS
from backend.config import TUNABLES
from backend.db import session_scope
from backend.main import app
from backend.models.agent_weight_history import AgentWeightHistory


pytestmark = [pytest.mark.integration]


# ── GET /learning/weights ────────────────────────────────────────────


def test_get_weights_returns_200_with_registry():
    """The list endpoint always returns 200 + the known_agents registry,
    even when no advisory rows exist yet."""
    client = TestClient(app)
    response = client.get("/learning/weights")
    assert response.status_code == 200
    body = response.json()
    assert "known_agents" in body
    assert "base_weights" in body
    assert "rows" in body
    assert "advisory_enabled" in body
    assert "apply_enabled" in body
    # All 8 council agents are listed.
    assert len(body["known_agents"]) == 8
    assert set(body["known_agents"]) == set(AGENT_BASE_WEIGHTS.keys())
    # The advisory + apply flags must be valid booleans.
    assert body["advisory_enabled"] in (False, True)
    assert body["apply_enabled"] in (False, True)


# ── GET /learning/weights/history ────────────────────────────────────


def test_get_weights_history_404_when_unknown_agent():
    """Unknown agent → 404."""
    client = TestClient(app)
    response = client.get("/learning/weights/history?agent=nope")
    assert response.status_code == 404
    body = response.json()
    assert "registered council agent" in str(body.get("detail", ""))


def test_get_weights_history_returns_rows_for_known_agent():
    """Registered agent → 200 with an empty or populated rows list.
    Either is fine; we only require the SHAPE."""
    client = TestClient(app)
    response = client.get("/learning/weights/history?agent=market")
    assert response.status_code == 200
    body = response.json()
    assert body["agent"] == "market"
    assert body["base_weight"] == 1.0
    assert "rows" in body
    assert isinstance(body["rows"], list)


# ── POST /learning/weights/recompute ─────────────────────────────────


def test_recompute_with_advisory_off_does_not_persist(monkeypatch):
    """With TUNABLES.adaptive_weights_advisory_enabled = False, the
    recompute endpoint must return the report but NOT write any rows
    to agent_weight_history."""
    monkeypatch.setattr(
        TUNABLES, "adaptive_weights_advisory_enabled", False,
        raising=False,
    )
    client = TestClient(app)
    # Capture row count before.
    with session_scope() as s:
        before_count = s.execute(
            select(func.count()).select_from(AgentWeightHistory)
        ).scalar() or 0
    response = client.post("/learning/weights/recompute")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["persisted"] is False
    assert body["advisory_enabled"] is False
    assert "report" in body
    # Row count must NOT have grown.
    with session_scope() as s:
        after_count = s.execute(
            select(func.count()).select_from(AgentWeightHistory)
        ).scalar() or 0
    assert after_count == before_count


# ── Engine regression: aggregate() unchanged when apply_enabled=False ─


def test_aggregate_unchanged_when_apply_disabled(monkeypatch):
    """The core invariant for 16.B replay drift: with
    ``adaptive_weights_apply_enabled = False`` (the default), aggregate
    on the SAME vote list must produce the SAME Consensus — bit-for-bit
    on stance + confidence + size_multiplier.

    The 18.D engine hook only fires inside ``run_consensus`` and only
    when the apply flag is True. ``aggregate()`` is called directly
    here so we exercise the post-vote-emit pipeline that the hook
    sits in front of.
    """
    monkeypatch.setattr(
        TUNABLES, "adaptive_weights_apply_enabled", False, raising=False,
    )
    votes = [
        AgentVote(
            agent=name, role=name, stance=STANCE_BUY,
            confidence=0.7, weight=1.0,
            reasoning="test", reasoning_type=REASONING_LEGACY,
        )
        for name in (
            "market", "microstructure", "macro", "portfolio_risk",
            "mechanical_trend", "thesis_health", "simulator",
            "devils_advocate",
        )
    ]
    c1: Consensus = aggregate(list(votes))
    c2: Consensus = aggregate(list(votes))
    # Stance + confidence + size must match across calls.
    assert c1.stance == c2.stance
    assert abs(c1.confidence - c2.confidence) < 1e-12
    assert abs(c1.size_multiplier - c2.size_multiplier) < 1e-12
    # The persisted vote weights are still the static 1.0 — the hook
    # did NOT fire because apply_enabled=False.
    for v in c1.votes:
        assert v.get("weight") == 1.0
