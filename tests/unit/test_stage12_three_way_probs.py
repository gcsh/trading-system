"""Stage-12.C7 Three-way probability head on Consensus."""
from backend.bot.agents import (
    AgentVote,
    STANCE_ABSTAIN,
    STANCE_BUY,
    STANCE_HOLD,
    STANCE_SELL,
    _three_way_probs,
    aggregate,
    run_consensus,
)


def _bullish_ctx():
    return {
        "ticker": "NVDA", "action": "BUY_CALL", "strategy": "trend_pullback",
        "analytics": {
            "regime": {"trend": "bullish", "volatility": "normal",
                         "gamma": "long_gamma", "momentum": "expanding"},
            "features": {"trend_bias": 0.5, "flow_bullishness": 0.4,
                            "premarket_bullish_sweeps": 0.6, "iv_rank": 30,
                            "pinning_probability": 0.1, "earnings_days": 30,
                            "vix": 14, "news_sentiment": 0.3, "volume_ratio": 1.4},
        },
        "snapshot": {"spy_trend": "bullish", "vix": 14, "volume": 1_400_000,
                       "avg_volume": 1_000_000},
        "cross_asset": {"equities": "risk_on", "volatility": "compressed"},
        "portfolio_risk": {"net_beta": 0.5, "drawdown_pct": 0.01,
                              "top_theme": "AI", "top_theme_pct": 0.18,
                              "concentration_flags": []},
        "cohort": {"win_rate": 0.62, "closed_count": 35},
    }


class TestThreeWayProbs:
    def test_sums_to_one(self):
        c = run_consensus(_bullish_ctx())
        p = c.probs
        s = p["long"] + p["short"] + p["abstain"]
        assert 0.99 <= s <= 1.01

    def test_bullish_context_long_dominant(self):
        c = run_consensus(_bullish_ctx())
        assert c.probs["long"] > c.probs["short"]
        assert c.probs["long"] > c.probs["abstain"]

    def test_empty_votes(self):
        p = _three_way_probs([])
        assert p == {"long": 0.0, "short": 0.0, "abstain": 1.0}

    def test_hold_splits_50_50(self):
        votes = [AgentVote("a", "A", STANCE_HOLD, 1.0, weight=1.0,
                              reasoning="x")]
        p = _three_way_probs(votes)
        assert p["long"] == 0.5
        assert p["short"] == 0.5
        assert p["abstain"] == 0.0

    def test_clear_buy(self):
        votes = [
            AgentVote("a", "A", STANCE_BUY, 0.8, weight=1.0, reasoning="x"),
            AgentVote("b", "B", STANCE_BUY, 0.7, weight=1.0, reasoning="x"),
        ]
        p = _three_way_probs(votes)
        assert p["long"] == 1.0
        assert p["short"] == 0.0
        assert p["abstain"] == 0.0

    def test_mixed_abstain_long(self):
        votes = [
            AgentVote("a", "A", STANCE_BUY, 0.6, weight=1.0, reasoning="x"),
            AgentVote("b", "B", STANCE_ABSTAIN, 0.6, weight=1.0, reasoning="x"),
        ]
        p = _three_way_probs(votes)
        # Equal contribution → 50/50 long/abstain
        assert p["long"] == 0.5
        assert p["abstain"] == 0.5

    def test_full_abstain_path_includes_probs(self):
        # Empty context → most agents abstain → recommendation == "abstain"
        c = run_consensus({"ticker": "X", "action": "BUY_STOCK"})
        assert c.recommendation == "abstain"
        assert c.probs["abstain"] > 0.0
        assert "probs" in c.to_dict()
