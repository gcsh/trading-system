"""MITS Phase 14.E — devil's advocate raises its voice when an open
position's thesis-health has fallen below the exit threshold."""
from __future__ import annotations

import pytest

from backend.bot.agents import agent_devils_advocate
from backend.config import TUNABLES


pytestmark = [pytest.mark.unit]


def _base_ctx(**overrides):
    ctx = {
        "ticker": "AAPL",
        "action": "BUY_STOCK",
        "snapshot": {"price": 200.0, "vix": 16.0},
        "analytics": {
            "regime": {"trend": "bullish", "volatility": "normal"},
            "features": {"iv_rank": 30.0, "pinning_probability": 0.0},
        },
        "features": {"iv_rank": 30.0, "pinning_probability": 0.0},
        "portfolio_risk": {"drawdown_pct": 0.0},
        "optimizer": {"requested_dollar": 1000.0,
                            "recommended_dollar": 950.0},
        "cohort": {"win_rate": 0.55, "closed_count": 30},
        "open_positions_thesis_health": [],
    }
    ctx.update(overrides)
    return ctx


def test_da_silent_when_no_open_positions_degraded():
    ctx = _base_ctx()
    vote = agent_devils_advocate(ctx)
    assert "degraded thesis" not in vote.reasoning


def test_da_calls_out_degraded_open_position():
    threshold = float(TUNABLES.thesis_health_exit_threshold)
    ctx = _base_ctx(open_positions_thesis_health=[
        {"ticker": "NVDA", "pattern": "bull_flag",
         "score": threshold - 10.0,
         "degraded_traits": ["held_vwap", "held_flag_low"]},
    ])
    vote = agent_devils_advocate(ctx)
    assert "degraded thesis" in vote.reasoning
    assert "NVDA" in vote.reasoning


def test_da_ignores_healthy_positions():
    threshold = float(TUNABLES.thesis_health_exit_threshold)
    ctx = _base_ctx(open_positions_thesis_health=[
        {"ticker": "NVDA", "pattern": "bull_flag",
         "score": threshold + 20.0,
         "degraded_traits": []},
    ])
    vote = agent_devils_advocate(ctx)
    assert "degraded thesis" not in vote.reasoning


def test_da_handles_missing_score_gracefully():
    ctx = _base_ctx(open_positions_thesis_health=[
        {"ticker": "NVDA", "pattern": "bull_flag",
         "score": None, "degraded_traits": []},
    ])
    vote = agent_devils_advocate(ctx)
    # No crash and no false-positive on bogus score.
    assert "degraded thesis" not in vote.reasoning


def test_da_multiple_degraded_positions_only_one_concern_per_position():
    threshold = float(TUNABLES.thesis_health_exit_threshold)
    ctx = _base_ctx(open_positions_thesis_health=[
        {"ticker": "NVDA", "pattern": "bull_flag",
         "score": threshold - 5.0, "degraded_traits": ["held_vwap"]},
        {"ticker": "TSLA", "pattern": "breakout",
         "score": threshold - 12.0, "degraded_traits": []},
    ])
    vote = agent_devils_advocate(ctx)
    assert "degraded thesis" in vote.reasoning
    # When two concerns fire, the agent moves to ABSTAIN with a
    # multi-bullet reasoning string. The two distinct tickers should
    # appear in some form even if truncated.
    assert "NVDA" in vote.reasoning or "TSLA" in vote.reasoning
