"""Adaptive strategy reads /learning/insights and down-weights failing combos."""
from backend.bot.strategies.adaptive import AdaptiveStrategy


def _two_strategy_scores(name_a, name_b, score_a, score_b):
    """Return a score_all stub that gives just two strategies the supplied scores
    and everything else 0 — keeps the test focused on the down-weighting logic."""
    def _score_all(_self, _ticker, _data):
        return {name_a: score_a, name_b: score_b}
    return _score_all


def test_adaptive_picks_runner_up_when_top_combo_is_failing(monkeypatch):
    """If macd_momentum::trending_up is flagged as failing, the planner should
    fall through to the next-best strategy in the regime preference list."""
    monkeypatch.setattr(AdaptiveStrategy, "detect_regime",
                         lambda self, _md: "trending_up")
    # Both strategies score equally well — without the penalty, macd_momentum
    # would win (it's first in the preference list).
    monkeypatch.setattr(AdaptiveStrategy, "score_all",
                         _two_strategy_scores("macd_momentum", "trend_pullback", 0.8, 0.8))
    # No DB writes; just supply the failing combos.
    monkeypatch.setattr("backend.bot.learning.insights",
                         lambda *a, **k: {"failing_combos": [
                             {"combo": "macd_momentum::trending_up",
                              "count": 8, "closed": 8, "wins": 1, "losses": 7,
                              "win_rate": 0.12, "total_pnl": -150.0},
                         ]})

    plan = AdaptiveStrategy().plan_day(["AAPL"], {})
    assert plan.primary_strategy != "macd_momentum"
    assert plan.primary_strategy == "trend_pullback"
    assert "down-weighted 1 failing combo" in plan.reason


def test_adaptive_unchanged_when_no_failing_combos(monkeypatch):
    monkeypatch.setattr(AdaptiveStrategy, "detect_regime",
                         lambda self, _md: "trending_up")
    monkeypatch.setattr(AdaptiveStrategy, "score_all",
                         _two_strategy_scores("macd_momentum", "trend_pullback", 0.8, 0.6))
    monkeypatch.setattr("backend.bot.learning.insights", lambda *a, **k: {"failing_combos": []})

    plan = AdaptiveStrategy().plan_day(["AAPL"], {})
    assert plan.primary_strategy == "macd_momentum"
    assert "down-weighted" not in plan.reason


def test_adaptive_safe_when_learning_layer_unavailable(monkeypatch):
    """A broken learning layer must NOT take down the planner."""
    monkeypatch.setattr(AdaptiveStrategy, "detect_regime",
                         lambda self, _md: "trending_up")
    monkeypatch.setattr(AdaptiveStrategy, "score_all",
                         _two_strategy_scores("macd_momentum", "trend_pullback", 0.8, 0.6))

    def _boom(*a, **k):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("backend.bot.learning.insights", _boom)

    plan = AdaptiveStrategy().plan_day(["AAPL"], {})
    assert plan.primary_strategy == "macd_momentum"     # planner still works
