"""MITS Phase 12.H — hierarchical aggregator unit tests.

Exercises ``_compute_hierarchical_priors`` directly (no DB) and
``_classify_confidence`` boundaries.
"""
from __future__ import annotations

import pytest

from backend.bot.corpus.knowledge_aggregator import (
    CONFIDENCE_HIGH_N, CONFIDENCE_LOW_N, CONFIDENCE_MEDIUM_N,
    _aggregate_members, _classify_confidence,
    _compute_hierarchical_priors,
)


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def test_classify_confidence_boundaries():
    assert _classify_confidence(0) == "thin"
    assert _classify_confidence(CONFIDENCE_LOW_N - 1) == "thin"
    assert _classify_confidence(CONFIDENCE_LOW_N) == "low"
    assert _classify_confidence(CONFIDENCE_MEDIUM_N - 1) == "low"
    assert _classify_confidence(CONFIDENCE_MEDIUM_N) == "medium"
    assert _classify_confidence(CONFIDENCE_HIGH_N - 1) == "medium"
    assert _classify_confidence(CONFIDENCE_HIGH_N) == "high"
    assert _classify_confidence(CONFIDENCE_HIGH_N + 500) == "high"


def test_hierarchical_priors_pooling():
    # Synthesise 100 rows across two tickers, same pattern, two regimes.
    rows = []
    for ticker in ("AAPL", "MSFT"):
        for i in range(50):
            rows.append({
                "ticker": ticker, "pattern": "vwap_reclaim",
                "regime": "trending_up" if i < 30 else "choppy",
                "vol_state": "normal", "time_bucket": "rth",
                "horizon": "5d",
                "return_pct": 0.01, "was_winner": (i % 3) != 0,
                "source": "historical_replay",
                "timestamp": None,
            })
    parents = _compute_hierarchical_priors(rows)
    # (pattern, regime) pool
    assert ("vwap_reclaim", "trending_up") in parents["pattern_regime"]
    assert ("vwap_reclaim", "choppy") in parents["pattern_regime"]
    trending_pool = parents["pattern_regime"][("vwap_reclaim", "trending_up")]
    assert trending_pool["n"] == 60  # 30 per ticker * 2
    # (pattern) pool aggregates everything.
    assert "vwap_reclaim" in parents["pattern"]
    assert parents["pattern"]["vwap_reclaim"]["n"] == 100


def test_aggregate_members_shrinks_to_parent_when_thin():
    """A 5-observation cell should shrink toward the parent (which
    has 800 observations + 60 percent win rate) rather than the
    academic prior (50 percent)."""
    members = [
        {"was_winner": True, "return_pct": 0.05, "source": "historical_replay"},
        {"was_winner": True, "return_pct": 0.05, "source": "historical_replay"},
        {"was_winner": True, "return_pct": 0.05, "source": "historical_replay"},
        {"was_winner": True, "return_pct": 0.05, "source": "historical_replay"},
        {"was_winner": True, "return_pct": 0.05, "source": "historical_replay"},
    ]
    parents = {
        "pattern_regime": {
            ("vwap_reclaim", "trending_up"): {"n": 800, "win_rate": 0.60},
        },
        "pattern": {
            "vwap_reclaim": {"n": 5000, "win_rate": 0.58},
        },
    }
    priors_by_pattern = {
        "vwap_reclaim": [
            {"cohort_descriptor": "any", "prior_win_rate": 0.50,
              "prior_weight": 10},
        ],
    }
    out = _aggregate_members(
        members, "vwap_reclaim", "trending_up", "5d",
        priors_by_pattern, split="combined",
        hierarchical_priors=parents,
    )
    assert out is not None
    # With parent shrinkage active, posterior should be well above the
    # 50 percent academic baseline because the parent says 60 percent.
    assert out["posterior_win_rate"] > 0.55
    assert out["confidence_level"] == "thin"


def test_aggregate_members_uses_pattern_parent_when_pr_thin():
    """When (pattern, regime) parent itself is too thin (<30), fall
    through to (pattern) parent."""
    members = [
        {"was_winner": True, "return_pct": 0.05, "source": "historical_replay"},
    ] * 5
    parents = {
        "pattern_regime": {
            ("vwap_reclaim", "trending_up"): {"n": 12, "win_rate": 0.55},
        },
        "pattern": {
            "vwap_reclaim": {"n": 4000, "win_rate": 0.62},
        },
    }
    priors_by_pattern = {
        "vwap_reclaim": [
            {"cohort_descriptor": "any", "prior_win_rate": 0.50,
              "prior_weight": 10},
        ],
    }
    out = _aggregate_members(
        members, "vwap_reclaim", "trending_up", "5d",
        priors_by_pattern, split="combined",
        hierarchical_priors=parents,
    )
    assert out is not None
    # Pattern parent (62 percent) should push the posterior toward 0.62.
    assert out["posterior_win_rate"] > 0.55
