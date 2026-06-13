"""MITS Phase 14.E — build_agent_context surfaces per-position thesis-health."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from backend.bot.agent_context import build_agent_context
from backend.bot.corpus.knowledge_aggregator import recompute_cells
from backend.bot.corpus.priors_loader import load_default_priors
from backend.bot.thesis.profile_builder import clear_profile_cache
from backend.db import init_db, session_scope
from backend.models.market_observation import MarketObservation
from backend.models.market_outcome import MarketOutcome
from backend.models.paper import PaperPosition


pytestmark = [pytest.mark.unit]


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
    clear_profile_cache()
    try:
        yield path
    finally:
        clear_profile_cache()
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


def _seed_winner_cohort(ticker: str, pattern: str,
                              n_winners: int = 30) -> None:
    """Seed enough winners for build_winner_profile to consider it
    trustworthy. Health calculator weights traits by feature presence
    on the observations."""
    with session_scope() as s:
        for i in range(n_winners):
            obs = MarketObservation(
                ticker=ticker, pattern=pattern,
                timestamp=datetime(2024, 1, 1) + timedelta(days=i),
                timeframe="1d", regime="trending_up", vol_state="normal",
                time_bucket="rth", spot=100.0,
                source="historical_replay",
                features=json.dumps({"price_vs_vwap": 0.5,
                                              "price_vs_flag_low": 0.5}),
            )
            s.add(obs)
            s.flush()
            s.add(MarketOutcome(
                observation_id=obs.id, horizon="1d",
                entry_price=100.0, exit_price=112.0,
                return_pct=0.12, was_winner=True,
            ))


def _seed_position(ticker: str, *, current_price: float, avg_cost: float,
                          vwap: float, flag_low: float, pattern: str,
                          regime: str = "trending_up") -> int:
    """Insert a paper position with the meta blob downstream code reads."""
    meta = {
        "pattern": pattern,
        "regime": regime,
        "current_price": current_price,
        "vwap": vwap,
        "flag_low": flag_low,
    }
    with session_scope() as s:
        pos = PaperPosition(
            ticker=ticker, kind="stock", quantity=10.0,
            avg_cost=avg_cost, meta=json.dumps(meta),
        )
        s.add(pos)
        s.flush()
        return pos.id


def test_context_includes_key_when_no_positions(fresh_db):
    ctx = build_agent_context(
        ticker="AAPL", action="BUY_STOCK", strategy="bull_flag",
        snapshot={"price": 200.0},
        analytics={"regime": {"trend": "bullish", "volatility": "normal"},
                       "features": {}},
    )
    assert "open_positions_thesis_health" in ctx
    assert ctx["open_positions_thesis_health"] == []


def test_context_populates_open_positions_thesis_health(fresh_db):
    load_default_priors()
    _seed_winner_cohort("AAPL", "bull_flag", n_winners=30)
    recompute_cells("AAPL")
    _seed_position(
        "AAPL", current_price=210.0, avg_cost=200.0,
        vwap=205.0, flag_low=199.0, pattern="bull_flag",
    )
    ctx = build_agent_context(
        ticker="AAPL", action="BUY_STOCK", strategy="bull_flag",
        snapshot={"price": 210.0},
        analytics={"regime": {"trend": "bullish", "volatility": "normal"},
                       "features": {}},
    )
    entries = ctx["open_positions_thesis_health"]
    assert isinstance(entries, list)
    assert any(e["ticker"] == "AAPL" for e in entries)
    e = next(x for x in entries if x["ticker"] == "AAPL")
    assert e["pattern"] == "bull_flag"
    assert isinstance(e["score"], float)
    assert 0.0 <= e["score"] <= 100.0
    assert "degraded_traits" in e
    assert isinstance(e["degraded_traits"], list)


def test_context_excludes_positions_without_pattern_hint(fresh_db):
    """Position with no pattern in meta → no health row (we can't
    score what we can't identify)."""
    load_default_priors()
    _seed_winner_cohort("AAPL", "bull_flag", n_winners=30)
    recompute_cells("AAPL")
    with session_scope() as s:
        s.add(PaperPosition(
            ticker="MSFT", kind="stock", quantity=5.0,
            avg_cost=300.0, meta=json.dumps({"foo": "bar"}),
        ))
    ctx = build_agent_context(
        ticker="AAPL", action="BUY_STOCK", strategy="bull_flag",
        snapshot={"price": 210.0},
        analytics={"regime": {"trend": "bullish", "volatility": "normal"},
                       "features": {}},
    )
    entries = ctx["open_positions_thesis_health"]
    assert all(e["ticker"] != "MSFT" for e in entries)
