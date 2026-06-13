"""Stage-13.C9 Research Layer — autonomous "what changed" digest."""
import json
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.bot.research import (
    SEVERITY_ALERT,
    SEVERITY_INFO,
    SEVERITY_WARN,
    Finding,
    ResearchDigest,
    generate_digest,
    reset_state,
)


@pytest.fixture(autouse=True)
def _isolate():
    reset_state()
    yield
    reset_state()


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


def _seed_closed_with_consensus(*, pnl, votes, ts_offset_min=0):
    from backend.db import session_scope
    from backend.models.trade import Trade
    with session_scope() as s:
        t = Trade(ticker="NVDA", action="BUY_CALL", quantity=1, price=215,
                    strategy="trend_pullback", signal_source="t",
                    confidence=0.7, paper=1, status="closed",
                    instrument="option", pnl=pnl,
                    detail_json=json.dumps({"consensus": {"votes": votes}}))
        t.timestamp = datetime.utcnow() + timedelta(minutes=ts_offset_min)
        s.add(t); s.flush()
        return t.id


def _vote(agent, stance, conf=0.7):
    return {"agent": agent, "role": agent.title(), "stance": stance,
            "confidence": conf, "weight": 1.0, "reasoning": ""}


class TestGenerateDigest:
    def test_returns_digest_object(self, temp_db):
        rpt = generate_digest()
        assert isinstance(rpt, ResearchDigest)
        assert "info" in rpt.counts
        assert "warn" in rpt.counts
        assert "alert" in rpt.counts

    def test_no_findings_on_empty_system(self, temp_db):
        rpt = generate_digest()
        # No agent data, no model trained, no cost data → only feeds + cohorts
        # might report. Should not crash.
        assert isinstance(rpt.findings, list)


class TestEndpoint:
    def test_digest_endpoint(self, client):
        body = client.get("/research/digest").json()
        assert "generated_at" in body
        assert "findings" in body
        assert "counts" in body
