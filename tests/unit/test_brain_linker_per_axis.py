"""MITS Phase 15.E — Per-component scoring on the brain linker."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from backend.db import init_db, session_scope
from backend.bot.scorecard import brain_linker
from backend.bot.scorecard.brain_linker import (
    _score_components,
    link_brain_predictions,
)
from backend.models.brain_prediction import BrainPrediction
from backend.models.trade import Trade


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


def _bullish_bars():
    base = datetime.utcnow() - timedelta(hours=2)
    # Climbing closes — forward trend = bullish.
    return [
        {"timestamp": base + timedelta(minutes=15 * i),
         "open": 100 + i, "high": 101 + i, "low": 99 + i,
         "close": 100 + (i * 1.5)}
        for i in range(8)
    ]


def test_regime_call_correct_set_when_regime_blob_present(fresh_db):
    regime_blob = {"trend": {"value": "bullish", "source": "regime",
                              "freshness_seconds": 0.0, "health": "green"}}
    pred_id = None
    with session_scope() as s:
        p = BrainPrediction(
            surface="analysis", ticker="AAPL", window="today",
            pattern="bull_flag",
            suggested_action="BUY_CALL", suggested_direction="long_call",
            suggested_strike=180.0, suggested_dte=30,
            posterior_at_decision=0.7, sample_size_at_decision=300,
            invalidation_json=json.dumps([]),
            thesis_paragraph="…",
            regime_at_decision=json.dumps(regime_blob),
            created_at=datetime.utcnow() - timedelta(hours=2),
        )
        s.add(p)
        s.flush()
        pred_id = p.id
        out = _score_components(p, None, _bullish_bars())
    # Climbing bars + trend=bullish → regime call correct.
    assert out["regime_call_correct"] is True
    # Other axes — no breakdown / no pnl — must stay None.
    assert out["technical_call_correct"] is None
    assert out["options_call_correct"] is None
    assert out["analog_call_correct"] is None
    assert out["strategy_call_correct"] is None
    assert pred_id is not None


def test_technical_call_correct_long_call_positive_pnl(fresh_db):
    with session_scope() as s:
        p = BrainPrediction(
            surface="analysis", ticker="AAPL", window="today",
            pattern="bull_flag",
            suggested_action="BUY_CALL", suggested_direction="long_call",
            posterior_at_decision=0.7, sample_size_at_decision=300,
            invalidation_json=json.dumps([]),
            actual_pnl_pct=0.05,  # +5% return
            created_at=datetime.utcnow() - timedelta(hours=2),
        )
        out = _score_components(p, None, [])
    assert out["technical_call_correct"] is True


def test_technical_call_correct_long_put_negative_pnl(fresh_db):
    with session_scope() as s:
        p = BrainPrediction(
            surface="analysis", ticker="AAPL", window="today",
            pattern="bear_flag",
            suggested_action="BUY_PUT", suggested_direction="long_put",
            posterior_at_decision=0.6, sample_size_at_decision=200,
            invalidation_json=json.dumps([]),
            actual_pnl_pct=-0.03,
            created_at=datetime.utcnow() - timedelta(hours=2),
        )
        out = _score_components(p, None, [])
    # long_put with negative pnl → technical call was wrong.
    assert out["technical_call_correct"] is False


def test_no_decision_time_fields_all_axes_none(fresh_db):
    with session_scope() as s:
        p = BrainPrediction(
            surface="analysis", ticker="AAPL", window="today",
            pattern="bull_flag",
            suggested_action="BUY_CALL", suggested_direction=None,
            posterior_at_decision=0.7, sample_size_at_decision=300,
            invalidation_json=json.dumps([]),
            created_at=datetime.utcnow() - timedelta(hours=2),
        )
        out = _score_components(p, None, [])
    assert all(v is None for v in out.values())


def test_strategy_call_correct_when_top_strat_and_pnl_present(fresh_db):
    top_strat = {"strategy_name": "long_call_directional",
                  "cohort_win_rate": 0.62, "cohort_n": 240}
    breakdown = {"options": 0.7, "historical_analog": 0.65,
                  "technical": 0.6, "market_structure": 0.5,
                  "simulator": 0.5, "macro": 0.5, "composite": 0.6}
    with session_scope() as s:
        t = Trade(
            ticker="AAPL", action="BUY", quantity=0, price=2.5,
            strategy="brain", signal_source="live_engine",
            instrument="option", option_type="call",
            strike=180.0, contracts=4, pnl=120.0, status="closed",
            timestamp=datetime.utcnow() - timedelta(hours=1),
        )
        s.add(t)
        s.flush()
        p = BrainPrediction(
            surface="analysis", ticker="AAPL", window="today",
            pattern="bull_flag",
            suggested_action="BUY_CALL", suggested_direction="long_call",
            posterior_at_decision=0.7, sample_size_at_decision=300,
            invalidation_json=json.dumps([]),
            actual_pnl_pct=0.04,
            confidence_breakdown_at_decision=json.dumps(breakdown),
            top_strategy_at_decision=json.dumps(top_strat),
            created_at=datetime.utcnow() - timedelta(hours=2),
        )
        out = _score_components(p, t, [])
    assert out["strategy_call_correct"] is True
    assert out["options_call_correct"] is True
    assert out["analog_call_correct"] is True
    assert out["technical_call_correct"] is True


def test_link_brain_predictions_populates_axes_on_resolve(fresh_db):
    regime_blob = {"trend": {"value": "bullish"}}
    with session_scope() as s:
        s.add(BrainPrediction(
            surface="analysis", ticker="AAPL", window="today",
            pattern="bull_flag",
            suggested_action="BUY_CALL", suggested_direction="long_call",
            suggested_strike=180.0, suggested_dte=30,
            posterior_at_decision=0.7, sample_size_at_decision=300,
            invalidation_json=json.dumps([]),
            thesis_paragraph="…",
            regime_at_decision=json.dumps(regime_blob),
            created_at=datetime.utcnow() - timedelta(hours=2),
        ))
        s.add(Trade(
            ticker="AAPL", action="BUY", quantity=0, price=2.5,
            strategy="brain", signal_source="live_engine",
            instrument="option", option_type="call",
            strike=180.0, contracts=4, pnl=120.0, status="closed",
            timestamp=datetime.utcnow() - timedelta(hours=1),
        ))
    with patch.object(brain_linker, "_fetch_bars_after",
                      return_value=_bullish_bars()):
        link_brain_predictions()
    with session_scope() as s:
        row = s.execute(
            __import__("sqlalchemy").select(BrainPrediction)
        ).scalars().first()
        assert row.actual_pnl_pct is not None and row.actual_pnl_pct > 0
        assert row.technical_call_correct is True
        assert row.regime_call_correct is True
