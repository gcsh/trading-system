"""Stage-12.A1 Agent Scorecards — per-agent accuracy from persisted consensus.

Pinned:
  • Empty system → every agent in roster with zero stats
  • Winner trade credits BUY voters, penalizes SELL voters, no penalty for HOLD
  • Loser trade credits SELL voters, penalizes BUY voters, rewards ABSTAIN
  • hit_rate excludes ABSTAIN from denominator
  • vote_weights shrinks smoothly toward 0.5 prior; engages without step-function
  • Endpoints surface the report
"""
import json
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.bot.agents.scorecard import (
    AgentScore,
    ScorecardReport,
    build_scorecard,
    vote_weights,
)


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


def _seed_trade(*, pnl, votes, trade_id=None, ts_offset_min=0):
    from backend.db import session_scope
    from backend.models.trade import Trade
    detail = {"consensus": {"votes": votes}}
    with session_scope() as s:
        t = Trade(ticker="NVDA", action="BUY_CALL", quantity=1, price=215,
                    strategy="trend_pullback", signal_source="t",
                    confidence=0.7, paper=1, status="closed",
                    instrument="option", pnl=pnl,
                    detail_json=json.dumps(detail))
        t.timestamp = datetime.utcnow() + timedelta(minutes=ts_offset_min)
        s.add(t); s.flush()
        return t.id


def _vote(agent, stance, conf=0.7, weight=1.0):
    return {"agent": agent, "role": agent.title(), "stance": stance,
            "confidence": conf, "weight": weight, "reasoning": ""}


class TestBuildScorecard:
    def test_empty_system_returns_seeded_roster(self, temp_db):
        rpt = build_scorecard()
        assert isinstance(rpt, ScorecardReport)
        assert rpt.closed_trades == 0
        # Every agent in AGENT_FUNCS appears even with zero stats.
        # Stage-17 consolidated panel = 5 distinct agents.
        names = {a.agent for a in rpt.agents}
        assert {"market", "microstructure", "macro",
                "portfolio_risk", "devils_advocate"}.issubset(names)
        for a in rpt.agents:
            assert a.decided_trades == 0
            assert a.hit_rate is None

    def test_winner_credits_buyers_penalizes_sellers(self, temp_db):
        _seed_trade(pnl=100.0, votes=[
            _vote("market", "buy"),
            _vote("flow", "sell"),
            _vote("risk", "abstain"),
        ])
        rpt = build_scorecard()
        by = {a.agent: a for a in rpt.agents}
        assert by["market"].correct == 1
        assert by["market"].wrong == 0
        assert by["market"].hit_rate == 1.0
        assert by["flow"].wrong == 1
        assert by["flow"].correct == 0
        assert by["flow"].hit_rate == 0.0
        assert by["risk"].abstain_count == 1
        assert by["risk"].missed_winners == 1
        assert by["risk"].hit_rate is None  # no decided

    def test_loser_credits_sellers_rewards_abstain(self, temp_db):
        _seed_trade(pnl=-80.0, votes=[
            _vote("market", "buy"),
            _vote("flow", "sell"),
            _vote("risk", "abstain"),
        ])
        rpt = build_scorecard()
        by = {a.agent: a for a in rpt.agents}
        assert by["market"].wrong == 1
        assert by["flow"].correct == 1
        assert by["risk"].avoided_losers == 1
        assert by["risk"].missed_winners == 0

    def test_hold_is_neutral(self, temp_db):
        _seed_trade(pnl=100.0, votes=[_vote("market", "hold")])
        _seed_trade(pnl=-50.0, votes=[_vote("market", "hold")], ts_offset_min=1)
        rpt = build_scorecard()
        by = {a.agent: a for a in rpt.agents}
        assert by["market"].decided_trades == 0
        assert by["market"].correct == 0
        assert by["market"].wrong == 0

    def test_pnl_attribution(self, temp_db):
        _seed_trade(pnl=200.0, votes=[_vote("market", "buy")])
        _seed_trade(pnl=-50.0, votes=[_vote("market", "sell")], ts_offset_min=1)
        rpt = build_scorecard()
        by = {a.agent: a for a in rpt.agents}
        # buy on +200 winner → +200; sell on -50 loser → +50 (avoided loss)
        assert by["market"].pnl_attributed == 250.0


class TestVoteWeights:
    def test_default_when_insufficient_data(self, temp_db):
        # Stage-16 — Bayesian shrinkage with prior_weight=20: 1 winning
        # trade shrinks to (1 + 10) / (1 + 20) ≈ 0.524 → only a tiny boost
        # away from the default 1.0.
        _seed_trade(pnl=100.0, votes=[_vote("market", "buy")])
        w = vote_weights(prior_weight=20)
        # Tiny boost, well within ~0.05 of default 1.0
        assert 0.95 < w["market"] < 1.05

    def test_boost_when_high_hit_rate(self, temp_db):
        # 50 winners → shrunken rate (50+10)/(50+20) ≈ 0.857 → strong boost
        for i in range(50):
            _seed_trade(pnl=50.0, votes=[_vote("market", "buy")],
                          ts_offset_min=i)
        w = vote_weights(prior_weight=20, default_weight=1.0,
                            max_boost=1.5)
        assert 1.0 < w["market"] <= 1.5

    def test_penalty_when_low_hit_rate(self, temp_db):
        # 50 losers + prior(20*0.5=10) → shrunken (0+10)/(50+20) ≈ 0.143
        # delta = -0.714 → weight ≈ 0.64 — pushed below 1.0 but the prior
        # keeps us off the 0.5 floor until we accumulate far more evidence.
        for i in range(50):
            _seed_trade(pnl=-50.0, votes=[_vote("market", "buy")],
                          ts_offset_min=i)
        w = vote_weights(prior_weight=20, default_weight=1.0,
                            max_penalty=0.5)
        assert 0.5 <= w["market"] < 0.85

    def test_penalty_floor_at_extreme_evidence(self, temp_db):
        # 500 losers + prior 20 → shrunken ≈ 0.019 → weight ≈ 0.52, right at
        # the floor. Shrinkage by design prevents the floor from triggering
        # on small-sample flukes — it converges to the floor only as
        # evidence overwhelms the prior.
        for i in range(500):
            _seed_trade(pnl=-50.0, votes=[_vote("market", "buy")],
                          ts_offset_min=i)
        w = vote_weights(prior_weight=20, default_weight=1.0,
                            max_penalty=0.5)
        assert 0.5 <= w["market"] < 0.55

    def test_shrinkage_engages_smoothly(self, temp_db):
        # Stage-16 — the whole point of shrinkage: 3 wins shouldn't blow
        # up weight, 30 wins should noticeably push it.
        for i in range(3):
            _seed_trade(pnl=50.0, votes=[_vote("market", "buy")],
                          ts_offset_min=i)
        w_small = vote_weights(prior_weight=20)
        assert 1.0 < w_small["market"] < 1.10        # subtle nudge

        # Add 27 more for 30 total wins → bigger but bounded
        for i in range(3, 30):
            _seed_trade(pnl=50.0, votes=[_vote("market", "buy")],
                          ts_offset_min=i)
        w_big = vote_weights(prior_weight=20)
        assert w_big["market"] > w_small["market"]


class TestEndpoints:
    def test_scorecard_endpoint(self, client):
        body = client.get("/agents/scorecard").json()
        assert "agents" in body
        assert "closed_trades" in body
        assert body["closed_trades"] == 0
        # All seven default agents listed.
        names = {a["agent"] for a in body["agents"]}
        assert "market" in names

    def test_weights_endpoint(self, client):
        body = client.get("/agents/weights").json()
        assert "weights" in body
        # Defaults (no decided data) → all 1.0
        for w in body["weights"].values():
            assert w == 1.0
