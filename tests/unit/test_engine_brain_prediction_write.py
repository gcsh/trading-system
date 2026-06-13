"""MITS Phase 15 follow-up Item 2 — engine cycle → BrainPrediction writer.

Covers the helper + the post-loop sweep:
- helper persists a row with all three JSON snapshots populated
- sweep writes ``surface='engine'`` rows for blocked-post-consensus events
  with ``linked_trade_id=None`` and ``outcome='not_traded'``
- sweep skips pre-consensus blocks (no ``consensus`` key on event)
- helper handles ``None`` inputs without raising
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest
from sqlalchemy import select

from backend.bot.engine import BotEngine
from backend.db import init_db, session_scope
from backend.models.brain_prediction import BrainPrediction


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
    try:
        yield
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


def _engine() -> BotEngine:
    """Construct a real BotEngine without touching the network — the
    helpers under test never reach the executor or market data."""
    return BotEngine()


def _regime_vector_blob() -> dict:
    """Return a regime_vector dict shaped like ``RegimeVector.to_dict``."""
    return {
        "ticker": "AAPL",
        "as_of": "2026-06-11T15:00:00",
        "trend": {"value": "trending_up", "health": "green"},
        "iv_rank": {"value": "low", "health": "green"},
        "iv_regime": {"value": "compression", "health": "green"},
        "intraday_regime": {"value": "normal", "health": "green"},
        "gamma_state": {"value": "positive", "health": "green"},
        "macro_regime": {"value": "risk_on", "health": "green"},
        "volatility_state": {"value": "normal", "health": "green"},
        "health": "green",
    }


def _confidence_breakdown_blob() -> dict:
    return {
        "composite": 0.62,
        "axis_health": {
            "market_structure": "green", "technical": "green",
            "options": "yellow", "historical_analog": "green",
            "simulator": "green", "macro": "green",
        },
        "axis_n": {
            "market_structure": 2, "technical": 2, "options": 1,
            "historical_analog": 1, "simulator": 1, "macro": 1,
        },
        "market_structure": 0.65, "technical": 0.55, "options": 0.48,
        "historical_analog": 0.62, "simulator": 0.60, "macro": 0.58,
    }


def _top_strategy_blob() -> dict:
    return {
        "id": "long_call_high_iv_low",
        "score": 0.71, "ev_score": 0.7, "regime_fit": 1.0,
        "analog_support": 0.65,
    }


def test_helper_persists_row_with_all_snapshots(fresh_db):
    """Direct helper call writes a row with all JSON snapshots populated."""
    # Seed a Trade so the linked_trade_id FK resolves.
    from backend.models.trade import Trade
    with session_scope() as s:
        s.add(Trade(
            ticker="AAPL", action="BUY_CALL", quantity=1.0, price=180.0,
            strategy="ai_brain", signal_source="engine", confidence=0.7,
            reason="test", paper=1, status="open", instrument="option",
        ))
        s.flush()
    engine = _engine()
    pred_id = engine._persist_brain_prediction_engine(
        ticker="AAPL",
        suggested_action="BUY_CALL",
        suggested_direction="long",
        confidence_self_assessment=0.61,
        invalidation=["below VWAP", "RSI<30"],
        thesis_paragraph="Council leans long on a coiling AAPL.",
        regime_vector=_regime_vector_blob(),
        confidence_breakdown=_confidence_breakdown_blob(),
        top_strategy=_top_strategy_blob(),
        linked_trade_id=1,
        outcome="pending",
    )
    assert isinstance(pred_id, int)
    with session_scope() as s:
        row = s.execute(select(BrainPrediction)).scalars().first()
        assert row is not None
        assert row.surface == "engine"
        assert row.ticker == "AAPL"
        assert row.suggested_action == "BUY_CALL"
        assert row.suggested_direction == "long"
        assert row.confidence_self_assessment == pytest.approx(0.61)
        assert row.linked_trade_id == 1
        assert row.outcome == "pending"
        cb = json.loads(row.confidence_breakdown_at_decision)
        rv = json.loads(row.regime_at_decision)
        ts = json.loads(row.top_strategy_at_decision)
        inv = json.loads(row.invalidation_json)
    for key in (
        "composite", "axis_health", "axis_n", "market_structure",
        "technical", "options", "historical_analog", "simulator", "macro",
    ):
        assert key in cb
    assert rv["ticker"] == "AAPL"
    assert rv["trend"]["value"] == "trending_up"
    assert ts["id"] == "long_call_high_iv_low"
    assert inv == ["below VWAP", "RSI<30"]


def test_sweep_writes_blocked_post_consensus_event(fresh_db):
    """Block event carrying a consensus dict produces a not_traded row."""
    engine = _engine()
    event = {
        "ticker": "NVDA",
        "action": "BUY_CALL",
        "status": "consensus_abstain",
        "reason": "council abstained 4 of 7",
        "consensus": {
            "confidence": 0.42,
            "confidence_breakdown": _confidence_breakdown_blob(),
        },
        "regime_vector": _regime_vector_blob(),
        "_signal_for_brain": {
            "action": "BUY_CALL",
            "confidence": 0.55,
            "reason": "bull flag fired",
            "invalidation": ["close below 50EMA"],
        },
    }
    engine._sweep_block_brain_predictions([event])

    with session_scope() as s:
        rows = s.execute(select(BrainPrediction)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.surface == "engine"
        assert row.ticker == "NVDA"
        assert row.suggested_action == "BUY_CALL"
        assert row.suggested_direction == "long"
        assert row.linked_trade_id is None
        assert row.outcome == "not_traded"
        assert row.confidence_self_assessment == pytest.approx(0.42)
        cb = json.loads(row.confidence_breakdown_at_decision)
        inv = json.loads(row.invalidation_json)
    assert cb["composite"] == pytest.approx(0.62)
    assert inv == ["close below 50EMA"]
    # Scratch key was popped so it doesn't leak into the UI payload.
    assert "_signal_for_brain" not in event


def test_sweep_skips_pre_consensus_block(fresh_db):
    """Pre-consensus blocks (no ``consensus`` key) write nothing."""
    engine = _engine()
    events = [
        {  # market_closed — never reached the council
            "ticker": "—", "action": "HOLD", "status": "market_closed",
        },
        {  # low_confidence — also pre-consensus, no consensus dict
            "ticker": "TSLA", "action": "BUY_STOCK", "status": "low_confidence",
        },
    ]
    engine._sweep_block_brain_predictions(events)
    with session_scope() as s:
        rows = s.execute(select(BrainPrediction)).scalars().all()
    assert rows == []


def test_sweep_skips_executed_event(fresh_db):
    """Executed events were already written from _finalize_execution;
    the sweep must not double-write them. It also pops the scratch key."""
    engine = _engine()
    event = {
        "ticker": "MSFT",
        "action": "BUY_STOCK",
        "status": "submitted",
        "trade_id": 7,
        "consensus": {
            "confidence": 0.71,
            "confidence_breakdown": _confidence_breakdown_blob(),
        },
        "regime_vector": _regime_vector_blob(),
        "_signal_for_brain": {
            "action": "BUY_STOCK", "confidence": 0.71,
            "reason": "consensus execute", "invalidation": None,
        },
    }
    engine._sweep_block_brain_predictions([event])
    with session_scope() as s:
        rows = s.execute(select(BrainPrediction)).scalars().all()
    assert rows == []
    assert "_signal_for_brain" not in event


def test_helper_tolerates_all_none_inputs(fresh_db):
    """Helper must not raise on a minimal call with mostly Nones."""
    engine = _engine()
    pred_id = engine._persist_brain_prediction_engine(
        ticker="SPY",
        suggested_action=None,
        suggested_direction=None,
    )
    assert isinstance(pred_id, int)
    with session_scope() as s:
        row = s.execute(select(BrainPrediction)).scalars().first()
        assert row is not None
        assert row.surface == "engine"
        assert row.outcome == "pending"
        assert row.confidence_breakdown_at_decision is None
        assert row.regime_at_decision is None
        assert row.top_strategy_at_decision is None
        assert row.invalidation_json is None
