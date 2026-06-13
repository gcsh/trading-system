"""Stage-15 — Claude-per-agent enrichment.

Pinned:
  • enrich() returns {} when no key + no client
  • enrich() with mocked client returns dict keyed by agent
  • run_consensus(enrich_with_claude=True) splices enrichment into reasoning
  • run_consensus(enrich_with_claude=False) leaves heuristic reasoning intact
  • Bad JSON from Claude → falls back gracefully
"""
import json
from unittest.mock import MagicMock

import pytest

from backend.bot.agents import (
    AgentVote,
    STANCE_BUY,
    run_consensus,
)
from backend.bot.agents.claude_voice import (
    AgentVoiceEnricher,
    reset_enricher,
)


@pytest.fixture(autouse=True)
def _isolate():
    reset_enricher()
    yield
    reset_enricher()


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = MagicMock(input_tokens=100, output_tokens=50)


class _Messages:
    def __init__(self, text):
        self._text = text
    def create(self, **kw):
        return _Resp(self._text)


class _Client:
    def __init__(self, text):
        self.messages = _Messages(text)


class TestEnricher:
    def test_no_key_no_client_returns_empty(self):
        e = AgentVoiceEnricher(api_key="")
        votes = [AgentVote("market", "M", STANCE_BUY, 0.7, weight=1.0,
                              reasoning="bullish bias")]
        out = e.enrich(votes=votes, context={"ticker": "NVDA"})
        assert out == {}

    def test_with_mocked_client_returns_dict(self):
        # Stage-17 — use consolidated agent names (microstructure, not flow)
        payload = json.dumps({
            "market": "Bullish 50/200 MA cross plus expanding ADX confirms regime.",
            "microstructure": "Dark-pool tape leans 18% bullish on the last hour.",
        })
        e = AgentVoiceEnricher(client=_Client(payload))
        votes = [
            AgentVote("market", "M", STANCE_BUY, 0.7, weight=1.0,
                        reasoning="bullish bias"),
            AgentVote("microstructure", "MS", STANCE_BUY, 0.65, weight=1.0,
                        reasoning="flow positive"),
        ]
        out = e.enrich(votes=votes, context={"ticker": "NVDA"})
        assert "market" in out
        assert "Bullish" in out["market"]
        assert "microstructure" in out

    def test_bad_json_returns_empty(self):
        e = AgentVoiceEnricher(client=_Client("not JSON"))
        votes = [AgentVote("market", "M", STANCE_BUY, 0.7, weight=1.0,
                              reasoning="x")]
        out = e.enrich(votes=votes, context={"ticker": "X"})
        assert out == {}

    def test_filters_unknown_agents(self):
        # Claude could return a key that's not one of our agents — skip it.
        payload = json.dumps({
            "market": "valid agent",
            "unknown_agent": "should be filtered out",
        })
        e = AgentVoiceEnricher(client=_Client(payload))
        votes = [AgentVote("market", "M", STANCE_BUY, 0.7, weight=1.0,
                              reasoning="x")]
        out = e.enrich(votes=votes, context={"ticker": "X"})
        assert "market" in out
        assert "unknown_agent" not in out


class TestConsensusEnrichment:
    def test_enrich_off_preserves_heuristic(self):
        # Default — no enrichment
        c = run_consensus({"ticker": "X", "action": "BUY_STOCK"},
                            enrich_with_claude=False)
        for vote in c.votes:
            # No enrichment separator inserted
            assert "\n➜" not in vote["reasoning"]

    def test_enrich_on_without_key_still_falls_back(self):
        # No Anthropic key configured → enrich is a no-op, no crash
        c = run_consensus({"ticker": "X", "action": "BUY_STOCK"},
                            enrich_with_claude=True)
        # STRAT.1 added mechanical_trend (6); MITS-5 added thesis_health (7);
        # Phase 14.C added simulator (8).
        assert len(c.votes) == 8

    def test_enrich_on_with_mock_appends_to_reasoning(self):
        """Inject a fake enricher so we don't need an Anthropic key."""
        # Stage-17: 8 → 5 agents. Use names from the consolidated panel.
        payload = json.dumps({
            "market": "extra market detail",
            "microstructure": "extra micro detail",
        })
        from backend.bot.agents import claude_voice
        claude_voice._ENRICHER = AgentVoiceEnricher(client=_Client(payload))

        ctx = {
            "ticker": "NVDA", "action": "BUY_CALL", "strategy": "trend_pullback",
            "analytics": {
                "regime": {"trend": "bullish", "volatility": "normal",
                              "gamma": "long_gamma", "momentum": "expanding"},
                "features": {"trend_bias": 0.5, "flow_bullishness": 0.4,
                                "iv_rank": 30, "vix": 14},
            },
            "snapshot": {"vix": 14, "volume": 1_400_000, "avg_volume": 1_000_000},
        }
        c = run_consensus(ctx, enrich_with_claude=True)
        by_agent = {v["agent"]: v for v in c.votes}
        assert "\n➜ extra market detail" in by_agent["market"]["reasoning"]
        assert "\n➜ extra micro detail" in by_agent["microstructure"]["reasoning"]
        # macro agent not in enrichment dict — its reasoning unchanged
        assert "\n➜" not in by_agent["macro"]["reasoning"]
