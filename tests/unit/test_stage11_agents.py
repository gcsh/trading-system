"""Stage-11.3 Multi-Agent AI — individual agents + consensus + endpoints.

Pinned:
  • Each agent returns a valid AgentVote with the right role/stance/conf
  • Agents abstain when data is missing (no fake confidence)
  • run_consensus returns a Consensus dict with every required field
  • An aligned-bullish context → recommendation == "execute"
  • A risk-off context → consensus opposes the long
  • Empty / hostile context → recommendation == "abstain"
  • GET /agents/list returns the seven agents
  • POST /agents/consensus/preview wraps run_consensus
  • GET /agents/consensus/{id} → 404 / 200 with persisted consensus
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from backend.bot.agents import (
    AgentVote,
    Consensus,
    STANCE_ABSTAIN,
    STANCE_BUY,
    STANCE_SELL,
    agent_execution,
    agent_flow,
    agent_macro,
    agent_market,
    agent_options,
    agent_portfolio,
    agent_risk,
    aggregate,
    list_agents,
    run_consensus,
)


# ── individual agents ────────────────────────────────────────────────────


def _bullish_ctx():
    return {
        "ticker": "NVDA", "action": "BUY_CALL", "strategy": "trend_pullback",
        "analytics": {
            "regime": {"trend": "bullish", "volatility": "normal",
                         "gamma": "long_gamma", "momentum": "expanding",
                         "label": "bullish · normal-vol · long gamma"},
            "features": {
                "trend_bias": 0.5, "flow_bullishness": 0.4,
                "premarket_bullish_sweeps": 0.6, "dealer_regime": "long_gamma",
                "hedging_pressure": "normal", "iv_rank": 30,
                "pinning_probability": 0.1, "earnings_days": 30,
                "vix": 14, "news_sentiment": 0.3, "volume_ratio": 1.4,
            },
        },
        "snapshot": {"spy_trend": "bullish", "vix": 14, "volume": 1_400_000,
                       "avg_volume": 1_000_000},
        "cross_asset": {"equities": "risk_on", "volatility": "compressed"},
        "portfolio_risk": {"net_beta": 0.5, "drawdown_pct": 0.01,
                              "top_theme": "AI infra", "top_theme_pct": 0.18,
                              "concentration_flags": []},
        "cohort": {"win_rate": 0.62, "closed_count": 35},
    }


def _bearish_ctx():
    return {
        "ticker": "NVDA", "action": "BUY_PUT", "strategy": "iv_short",
        "analytics": {
            "regime": {"trend": "bearish", "volatility": "elevated",
                         "gamma": "short_gamma", "momentum": "contracting",
                         "label": "bearish · elevated · short gamma"},
            "features": {
                "trend_bias": -0.5, "flow_bullishness": -0.5,
                "premarket_bullish_sweeps": -0.5, "iv_rank": 40,
                "pinning_probability": 0.1, "vix": 26,
                "news_sentiment": -0.4, "volume_ratio": 1.4,
            },
        },
        "snapshot": {"spy_trend": "bearish", "vix": 26, "volume": 1_400_000,
                       "avg_volume": 1_000_000},
        "cross_asset": {"equities": "risk_off", "volatility": "spiking"},
        "portfolio_risk": {"net_beta": 0.0, "drawdown_pct": 0.02,
                              "top_theme": "AI", "top_theme_pct": 0.20,
                              "concentration_flags": []},
        "cohort": {"win_rate": 0.55, "closed_count": 20},
    }


class TestIndividualAgents:
    def test_market_supports_aligned_long(self):
        v = agent_market(_bullish_ctx())
        assert isinstance(v, AgentVote)
        assert v.agent == "market"
        assert v.stance == STANCE_BUY
        assert v.confidence > 0.6

    def test_market_abstains_in_chop(self):
        ctx = _bullish_ctx()
        ctx["analytics"]["regime"]["trend"] = "choppy"
        ctx["analytics"]["features"]["trend_bias"] = 0.05
        v = agent_market(ctx)
        assert v.stance == STANCE_ABSTAIN

    def test_flow_abstains_with_no_data(self):
        ctx = {"action": "BUY_CALL"}
        v = agent_flow(ctx)
        assert v.stance == STANCE_ABSTAIN
        assert "no flow" in v.reasoning.lower()

    def test_flow_supports_when_aligned(self):
        v = agent_flow(_bullish_ctx())
        assert v.stance == STANCE_BUY

    def test_options_hostile_to_long_premium_when_iv_high(self):
        ctx = _bullish_ctx()
        ctx["analytics"]["features"]["iv_rank"] = 85
        v = agent_options(ctx)
        assert v.stance in (STANCE_SELL, STANCE_ABSTAIN)
        assert "iv" in v.reasoning.lower()

    def test_macro_sells_long_into_risk_off(self):
        v = agent_macro(_bearish_ctx())
        # BUY_PUT direction is short — risk-off supports it
        assert v.stance == STANCE_SELL
        ctx = _bullish_ctx()
        ctx["snapshot"]["vix"] = 28
        ctx["snapshot"]["spy_trend"] = "bearish"
        ctx["cross_asset"]["equities"] = "risk_off"
        ctx["analytics"]["features"]["vix"] = 28
        v2 = agent_macro(ctx)
        # BUY_CALL into risk-off → SELL stance
        assert v2.stance == STANCE_SELL

    def test_risk_abstains_in_drawdown(self):
        ctx = _bullish_ctx()
        ctx["portfolio_risk"]["drawdown_pct"] = 0.12
        v = agent_risk(ctx)
        assert v.stance == STANCE_ABSTAIN
        assert "drawdown" in v.reasoning.lower()

    def test_portfolio_abstains_when_theme_hot(self):
        ctx = _bullish_ctx()
        ctx["portfolio_risk"]["top_theme_pct"] = 0.55
        v = agent_portfolio(ctx)
        assert v.stance == STANCE_ABSTAIN

    def test_execution_abstains_in_thin_tape(self):
        ctx = _bullish_ctx()
        ctx["snapshot"]["volume"] = 200_000
        ctx["analytics"]["features"]["volume_ratio"] = 0.2
        v = agent_execution(ctx)
        assert v.stance == STANCE_ABSTAIN


# ── consensus engine ────────────────────────────────────────────────────


class TestConsensus:
    def test_aligned_bullish_context_executes(self):
        c = run_consensus(_bullish_ctx())
        assert isinstance(c, Consensus)
        assert c.stance == STANCE_BUY
        assert c.recommendation == "execute"
        assert c.size_multiplier > 0.7
        assert c.confidence > 0.5
        # STRAT.1 added mechanical_trend (6); MITS-5 added thesis_health (7);
        # Phase 14.C added simulator (8).
        assert len(c.votes) == 8

    def test_short_aligned_context_executes_short(self):
        c = run_consensus(_bearish_ctx())
        # bearish ctx is a BUY_PUT — supportive agents back STANCE_SELL
        assert c.stance == STANCE_SELL
        assert c.recommendation in ("execute", "size_down")

    def test_empty_context_abstains(self):
        c = run_consensus({"ticker": "X", "action": "BUY_STOCK"})
        # Most agents will abstain on missing data → abstain threshold trips
        assert c.recommendation == "abstain"

    def test_hostile_context_opposes(self):
        ctx = _bullish_ctx()
        ctx["portfolio_risk"]["drawdown_pct"] = 0.12          # risk: abstain
        ctx["portfolio_risk"]["top_theme_pct"] = 0.6          # portfolio: abstain
        ctx["analytics"]["features"]["iv_rank"] = 90          # options hostile
        ctx["snapshot"]["volume"] = 100_000                   # execution abstain
        ctx["analytics"]["features"]["volume_ratio"] = 0.15
        c = run_consensus(ctx)
        assert c.recommendation == "abstain"

    def test_aggregate_handles_empty_votes(self):
        c = aggregate([])
        assert c.stance == STANCE_ABSTAIN
        assert c.recommendation == "abstain"

    def test_disagreement_triggers_size_down(self):
        # Hand-craft a 50/50 split with high confidence on both sides.
        votes = [
            AgentVote("a", "A", STANCE_BUY, 0.90, weight=1.0,
                        reasoning="strong buy"),
            AgentVote("b", "B", STANCE_BUY, 0.40, weight=1.0,
                        reasoning="weak buy"),
            AgentVote("c", "C", STANCE_SELL, 0.85, weight=1.0,
                        reasoning="strong sell"),
            AgentVote("d", "D", STANCE_SELL, 0.30, weight=1.0,
                        reasoning="weak sell"),
        ]
        c = aggregate(votes, disagreement_threshold=0.10)
        # 2 dissenters and disagreement > threshold → size_down
        assert c.recommendation == "size_down"
        assert 0 < c.size_multiplier < 1.0


# ── endpoints ────────────────────────────────────────────────────────────


@pytest.fixture
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload
    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


class TestAgentEndpoints:
    def test_list_returns_eight_agents(self, client):
        # Stage-17: consolidated from 8 → 5 to reduce bureaucracy.
        # STRAT.1 (2026-06-04): added mechanical_trend as the deterministic
        # rule-based vote, taking the panel to 6.
        # MITS-5 (2026-06-05): added thesis_health, the 7th agent
        # (winner-trajectory exit monitor).
        # Phase 14.C: added simulator (forward payoff Monte Carlo), the 8th.
        body = client.get("/agents/list").json()
        agents = body["agents"]
        assert len(agents) == 8
        names = {a["agent"] for a in agents}
        assert names == {"market", "microstructure", "macro",
                            "portfolio_risk", "mechanical_trend",
                            "thesis_health", "simulator",
                            "devils_advocate"}

    def test_preview_returns_consensus(self, client):
        body = client.post("/agents/consensus/preview", json={
            "ticker": "NVDA", "action": "BUY_CALL",
            "analytics": _bullish_ctx()["analytics"],
            "portfolio_risk": _bullish_ctx()["portfolio_risk"],
            "snapshot": _bullish_ctx()["snapshot"],
            "cross_asset": _bullish_ctx()["cross_asset"],
        }).json()
        c = body["consensus"]
        assert c["stance"] == STANCE_BUY
        assert c["recommendation"] == "execute"
        # MITS-5 added thesis_health (7); Phase 14.C added simulator (8).
        assert len(c["votes"]) == 8

    def test_get_consensus_404_unknown(self, client):
        assert client.get("/agents/consensus/999999").status_code == 404

    def test_get_consensus_404_no_persistence(self, client):
        from backend.db import session_scope
        from backend.models.trade import Trade
        with session_scope() as s:
            t = Trade(ticker="X", action="BUY_STOCK", quantity=1, price=100,
                       strategy="s", signal_source="t", confidence=0.7,
                       paper=1, status="open", instrument="stock")
            s.add(t); s.flush()
            tid = t.id
        assert client.get(f"/agents/consensus/{tid}").status_code == 404

    def test_get_consensus_200_when_persisted(self, client):
        from backend.db import session_scope
        from backend.models.trade import Trade
        consensus_dict = {
            "stance": STANCE_BUY, "confidence": 0.7,
            "disagreement_score": 0.1, "recommendation": "execute",
            "size_multiplier": 0.9, "abstain_count": 0,
            "supporters": ["market", "flow"], "dissenters": [],
            "votes": [],
        }
        with session_scope() as s:
            t = Trade(ticker="NVDA", action="BUY_CALL", quantity=1, price=215,
                       strategy="x", signal_source="t", confidence=0.7,
                       paper=1, status="open", instrument="option",
                       detail_json=json.dumps({"consensus": consensus_dict}))
            s.add(t); s.flush()
            tid = t.id
        body = client.get(f"/agents/consensus/{tid}").json()
        assert body["trade_id"] == tid
        assert body["consensus"]["recommendation"] == "execute"
        assert body["consensus"]["supporters"] == ["market", "flow"]
