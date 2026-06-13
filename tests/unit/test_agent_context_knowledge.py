"""MITS Phase 1 — agent_context knowledge_evidence wiring tests.

Validates that:
  1. `load_knowledge_evidence` returns the empty shape when the corpus
     is cold, and a populated shape after observations + cells exist.
  2. `build_agent_context` exposes `knowledge_evidence` on the context.
  3. `apply_memory_bias` shifts vote confidence + annotates reasoning
     when the knowledge_evidence cells have a supportive (or opposing)
     posterior aggregate.
  4. The agent_market function — when run against a context that has a
     supportive `knowledge_evidence` summary — surfaces that evidence in
     the vote's reasoning string after memory bias is applied.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pytest

from backend.bot.agent_context import (
    apply_memory_bias, build_agent_context, load_knowledge_evidence,
)
from backend.bot.corpus.knowledge_aggregator import recompute_cells
from backend.bot.corpus.priors_loader import load_default_priors
from backend.db import init_db, session_scope
from backend.models.knowledge_graph_cell import KnowledgeGraphCell
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


@pytest.fixture
def fresh_db():
    import backend.db as db_mod
    prev_engine = db_mod._engine
    prev_session = db_mod._SessionLocal
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db(path)
    try:
        yield path
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


def _seed_cohort(ticker: str, pattern: str, regime: str,
                       n_winners: int, n_losers: int,
                       horizon: str = "1d",
                       source: str = "historical_replay") -> None:
    with session_scope() as s:
        for i in range(n_winners + n_losers):
            obs = MarketObservation(
                ticker=ticker, pattern=pattern,
                timestamp=datetime(2024, 1, 1) + timedelta(days=i),
                timeframe="1d", regime=regime, vol_state="normal",
                time_bucket="rth", spot=100.0, source=source,
            )
            s.add(obs)
            s.flush()
            is_winner = i < n_winners
            s.add(MarketOutcome(
                observation_id=obs.id, horizon=horizon,
                entry_price=100.0,
                exit_price=110.0 if is_winner else 90.0,
                return_pct=0.10 if is_winner else -0.10,
                was_winner=is_winner,
            ))


# ── load_knowledge_evidence ──────────────────────────────────────────


def test_load_knowledge_evidence_empty_when_cold(fresh_db):
    ke = load_knowledge_evidence(
        ticker="XYZ", regime="trending_up", vol_state="normal",
    )
    assert ke == {"cells": [], "summary": "", "most_similar_outcomes": []}


def test_load_knowledge_evidence_populated_after_seed(fresh_db):
    load_default_priors()
    _seed_cohort("AAPL", "bull_flag", "trending_up",
                       n_winners=15, n_losers=5)
    recompute_cells("AAPL")
    ke = load_knowledge_evidence(
        ticker="AAPL", regime="trending_up", vol_state="normal",
    )
    assert ke["cells"], "evidence should surface cells after seed"
    assert ke["summary"], "summary line should be non-empty"
    assert "analogs" in ke["summary"]
    # We seeded 15W / 5L → frequentist WR 75%, summary references that.
    assert any(c["sample_size"] >= 10 for c in ke["cells"])


def test_load_knowledge_evidence_falls_back_to_ticker_only(fresh_db):
    """When exact (regime, vol, bucket) yields nothing but the ticker has
    cells, the loader returns those rather than empty."""
    load_default_priors()
    _seed_cohort("MSFT", "breakout", "trending_up",
                       n_winners=8, n_losers=2)
    recompute_cells("MSFT")
    ke = load_knowledge_evidence(
        ticker="MSFT", regime="choppy", vol_state="high",
    )
    # Loose fallback returns ticker+horizon-only matches.
    assert ke["cells"], "loose fallback must surface ticker-level cells"


# ── build_agent_context exposes the block ────────────────────────────


def test_build_agent_context_includes_knowledge_evidence(fresh_db):
    load_default_priors()
    _seed_cohort("TSLA", "bull_flag", "trending_up",
                       n_winners=12, n_losers=8)
    recompute_cells("TSLA")
    ctx = build_agent_context(
        ticker="TSLA", action="BUY_STOCK", strategy="bull_flag",
        snapshot={"price": 200.0},
        analytics={"regime": {"trend": "trending_up", "volatility": "normal"},
                       "features": {}},
    )
    assert "knowledge_evidence" in ctx
    ke = ctx["knowledge_evidence"]
    assert isinstance(ke, dict)
    assert "cells" in ke and "summary" in ke and "most_similar_outcomes" in ke
    assert ke["cells"], "context should carry populated cells when corpus has data"


# ── apply_memory_bias uses knowledge_evidence ─────────────────────────


def _fake_vote(agent: str, confidence: float = 0.50):
    class V:
        pass
    v = V()
    v.agent = agent
    v.confidence = confidence
    v.reasoning = "base reason"
    return v


def test_apply_memory_bias_supports_when_posterior_strong(fresh_db):
    """A knowledge_evidence block with a strong aggregate posterior (>=
    0.65 on >=5 samples) must lift vote confidence and tag the reason."""
    ctx = {
        "knowledge_evidence": {
            "cells": [
                {"sample_size": 20, "posterior_win_rate": 0.72,
                 "win_rate": 0.75, "avg_return_pct": 0.03,
                 "avg_hold_minutes": 60, "horizon": "1d",
                 "pattern": "bull_flag"},
            ],
            "summary": "20 analogs, WR 75% (posterior 72%), avg move +3.0%",
            "most_similar_outcomes": [],
        },
    }
    v = _fake_vote("market", 0.50)
    apply_memory_bias([v], ctx)
    assert v.confidence > 0.50, "supportive evidence should lift confidence"
    bias = getattr(v, "memory_bias", None)
    assert bias is not None
    assert any("knowledge_supports" in r for r in bias["reasons"])
    # Reasoning gets annotated with the corpus summary the first time
    # the memory bias touches the vote.
    assert "knowledge:" in v.reasoning


def test_apply_memory_bias_opposes_when_posterior_weak(fresh_db):
    """Posterior aggregate at/below 0.40 must shave confidence.

    MITS P2.3: the calibrated derive_bias_factor needs sample_size
    >= TUNABLES.memory_bias_min_samples (default 20) to engage. Below
    that the bias stays neutral so a few thin observations don't sway
    confidence. This test seeds 30 samples to clear the floor.
    """
    ctx = {
        "knowledge_evidence": {
            "cells": [
                {"sample_size": 30, "posterior_win_rate": 0.30,
                 "win_rate": 0.27, "avg_return_pct": -0.02,
                 "avg_hold_minutes": 30, "horizon": "1d",
                 "pattern": "breakout"},
            ],
            "summary": "30 analogs, WR 27% (posterior 30%), avg move -2.0%",
            "most_similar_outcomes": [],
        },
    }
    v = _fake_vote("market", 0.60)
    apply_memory_bias([v], ctx)
    assert v.confidence < 0.60, "opposing evidence should shave confidence"
    bias = getattr(v, "memory_bias", None)
    assert bias is not None
    assert any("knowledge_opposes" in r for r in bias["reasons"])


def test_apply_memory_bias_neutral_when_thin_corpus(fresh_db):
    """Below 5 total samples we don't bias — corpus too thin to weigh."""
    ctx = {
        "knowledge_evidence": {
            "cells": [
                {"sample_size": 2, "posterior_win_rate": 0.90,
                 "win_rate": 1.0, "avg_return_pct": 0.05,
                 "avg_hold_minutes": 60, "horizon": "1d",
                 "pattern": "bull_flag"},
            ],
            "summary": "2 analogs, WR 100%",
            "most_similar_outcomes": [],
        },
    }
    v = _fake_vote("market", 0.50)
    apply_memory_bias([v], ctx)
    assert v.confidence == pytest.approx(0.50, abs=0.001), (
        "thin corpus should not bias")
    bias = getattr(v, "memory_bias", None)
    # Either no memory_bias attr at all (no reasons fired) or no
    # knowledge_* reason in it.
    if bias is not None:
        assert not any("knowledge_" in r for r in bias["reasons"])


def test_agent_market_carries_evidence_into_reasoning(fresh_db):
    """End-to-end: feed a context with supportive evidence to agent_market
    (via run_consensus's memory_bias hook) and verify the final vote's
    reasoning includes the knowledge summary."""
    from backend.bot.agents import agent_market
    from backend.bot.agent_context import apply_memory_bias as _amb
    ctx = {
        "ticker": "AAPL",
        "action": "BUY_STOCK",
        "analytics": {
            "regime": {"trend": "bullish", "volatility": "normal",
                          "momentum": "expanding"},
            "features": {"trend_bias": 0.4},
        },
        "features": {"trend_bias": 0.4},
        "knowledge_evidence": {
            "cells": [
                {"sample_size": 30, "posterior_win_rate": 0.70,
                 "win_rate": 0.73, "avg_return_pct": 0.025,
                 "avg_hold_minutes": 90, "horizon": "1d",
                 "pattern": "bull_flag"},
            ],
            "summary": "30 analogs, WR 73% (posterior 70%), avg move +2.5%",
            "most_similar_outcomes": [],
        },
    }
    vote = agent_market(ctx)
    base_conf = vote.confidence
    _amb([vote], ctx)
    assert vote.confidence > base_conf
    assert "knowledge:" in vote.reasoning
