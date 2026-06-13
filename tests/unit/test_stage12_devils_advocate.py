"""Stage-12.A2 Devil's Advocate agent — dedicated red-team voice.

Pinned:
  • Clean setup → HOLD (no opposition, just no flags)
  • Earnings within 3 days → at least one concern recorded
  • Long signal in bearish tape → opposes
  • Two adverse factors → ABSTAIN with high confidence
  • Optimizer big cut → noted as a concern
  • Agent appears in AGENT_FUNCS roster
"""
from backend.bot.agents import (
    AGENT_FUNCS,
    STANCE_ABSTAIN,
    STANCE_HOLD,
    STANCE_SELL,
    agent_devils_advocate,
)


def _ctx(**overrides):
    base = {
        "ticker": "NVDA", "action": "BUY_CALL", "strategy": "trend_pullback",
        "analytics": {
            "regime": {"trend": "bullish", "volatility": "normal",
                         "gamma": "long_gamma"},
            "features": {"iv_rank": 30, "pinning_probability": 0.1,
                            "earnings_days": 30, "vix": 14},
        },
        "snapshot": {"vix": 14},
        "optimizer": {"requested_dollar": 1000, "recommended_dollar": 950,
                        "drawdown_pct": 0.01},
        "portfolio_risk": {"drawdown_pct": 0.01, "concentration_flags": []},
        "cohort": {"win_rate": 0.6, "closed_count": 30},
    }
    base.update(overrides)
    return base


class TestDevilsAdvocate:
    def test_clean_setup_holds(self):
        v = agent_devils_advocate(_ctx())
        assert v.agent == "devils_advocate"
        assert v.stance == STANCE_HOLD
        assert "clean" in v.reasoning.lower()

    def test_earnings_within_three_days_flags(self):
        ctx = _ctx()
        ctx["analytics"]["features"]["earnings_days"] = 2
        v = agent_devils_advocate(ctx)
        assert "earnings" in v.reasoning.lower()

    def test_long_in_bearish_tape_opposes(self):
        ctx = _ctx()
        ctx["analytics"]["regime"]["trend"] = "bearish"
        v = agent_devils_advocate(ctx)
        # Only one concern → opposing vote
        assert v.stance == STANCE_SELL
        assert "long signal" in v.reasoning.lower()

    def test_two_concerns_abstain_loudly(self):
        ctx = _ctx()
        ctx["analytics"]["features"]["earnings_days"] = 1
        ctx["analytics"]["features"]["iv_rank"] = 90
        v = agent_devils_advocate(ctx)
        assert v.stance == STANCE_ABSTAIN
        assert v.confidence >= 0.65
        assert "red-team" in v.reasoning.lower()

    def test_optimizer_cut_is_a_concern(self):
        ctx = _ctx()
        ctx["optimizer"]["recommended_dollar"] = 200    # 80% cut
        # Pair with another flag to trigger abstain
        ctx["analytics"]["features"]["pinning_probability"] = 0.8
        v = agent_devils_advocate(ctx)
        assert v.stance == STANCE_ABSTAIN
        assert "optimizer" in v.reasoning.lower()

    def test_cohort_cold_with_enough_trades(self):
        ctx = _ctx()
        ctx["cohort"] = {"win_rate": 0.30, "closed_count": 20}
        # Pair with another to trigger abstain
        ctx["analytics"]["features"]["earnings_days"] = 1
        v = agent_devils_advocate(ctx)
        assert v.stance == STANCE_ABSTAIN

    def test_is_in_canonical_roster(self):
        names = {n for n, _, _ in AGENT_FUNCS}
        assert "devils_advocate" in names
