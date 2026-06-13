"""MITS Phase 14.D — Brain linker tests."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from backend.db import init_db, session_scope
from backend.bot.scorecard import brain_linker
from backend.bot.scorecard.brain_linker import link_brain_predictions
from backend.models.brain_prediction import (
    BrainPrediction,
    OUTCOME_LOSS,
    OUTCOME_NOT_TRADED,
    OUTCOME_PENDING,
    OUTCOME_WIN,
)
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


def _seed_pred(ticker="AAPL", action="BUY_CALL", direction="long_call",
               created_offset_hours=2,
               invalidation=None):
    with session_scope() as s:
        p = BrainPrediction(
            surface="analysis",
            ticker=ticker,
            window="today",
            pattern="bull_flag",
            suggested_action=action,
            suggested_direction=direction,
            suggested_strike=180.0,
            suggested_dte=30,
            posterior_at_decision=0.71,
            sample_size_at_decision=412,
            confidence_self_assessment=0.65,
            invalidation_json=json.dumps(invalidation or []),
            thesis_paragraph="…",
            created_at=datetime.utcnow() - timedelta(hours=created_offset_hours),
        )
        s.add(p)
        s.flush()
        return p.id


def _seed_trade(ticker="AAPL", option_type="call", pnl=120.0,
                price=2.5, contracts=4, status="closed",
                ts_offset_hours=1):
    with session_scope() as s:
        t = Trade(
            ticker=ticker, action="BUY", quantity=0, price=price,
            strategy="brain", signal_source="live_engine",
            instrument="option", option_type=option_type,
            strike=180.0, contracts=contracts,
            pnl=pnl, status=status,
            timestamp=datetime.utcnow() - timedelta(hours=ts_offset_hours),
        )
        s.add(t)
        s.flush()
        return t.id


def test_link_pending_to_matching_winning_trade(fresh_db):
    pred_id = _seed_pred()
    trade_id = _seed_trade(pnl=120.0)  # positive pnl → win
    with patch.object(brain_linker, "_fetch_bars_after", return_value=[]):
        stats = link_brain_predictions()
    assert stats["linked"] == 1
    assert stats["resolved"] == 1
    with session_scope() as s:
        row = s.get(BrainPrediction, pred_id)
        assert row.linked_trade_id == trade_id
        assert row.outcome == OUTCOME_WIN
        assert row.actual_pnl_pct is not None and row.actual_pnl_pct > 0
        assert row.resolved_at is not None


def test_link_pending_to_losing_trade(fresh_db):
    pred_id = _seed_pred()
    _seed_trade(pnl=-80.0)
    with patch.object(brain_linker, "_fetch_bars_after", return_value=[]):
        link_brain_predictions()
    with session_scope() as s:
        row = s.get(BrainPrediction, pred_id)
        assert row.outcome == OUTCOME_LOSS
        assert row.actual_pnl_pct is not None and row.actual_pnl_pct < 0


def test_link_skips_when_action_mismatches_trade_direction(fresh_db):
    pred_id = _seed_pred(action="BUY_PUT", direction="long_put")
    _seed_trade(option_type="call")  # mismatch
    with patch.object(brain_linker, "_fetch_bars_after", return_value=[]):
        stats = link_brain_predictions()
    assert stats["linked"] == 0
    with session_scope() as s:
        row = s.get(BrainPrediction, pred_id)
        assert row.linked_trade_id is None
        # Still pending — not in not_traded window yet.
        assert row.outcome == OUTCOME_PENDING


def test_stale_prediction_resolved_as_not_traded(fresh_db):
    # 60 hours old, no matching trade → not_traded.
    _seed_pred(created_offset_hours=60)
    with patch.object(brain_linker, "_fetch_bars_after", return_value=[]):
        stats = link_brain_predictions()
    assert stats["not_traded"] == 1
    with session_scope() as s:
        row = s.execute(select(BrainPrediction)).scalars().first()
        assert row.outcome == OUTCOME_NOT_TRADED


def test_invalidation_hit_detected_from_replayed_bars(fresh_db):
    """When invalidation bullet mentions 'below VWAP' and the replayed
    bars show a close beneath the running VWAP, the linker should flag
    invalidation_hit=True."""
    pred_id = _seed_pred(
        invalidation=["Position closes below VWAP", "Volume dries up"]
    )
    _seed_trade(pnl=10.0)
    base = datetime.utcnow() - timedelta(hours=1)
    # First two bars climb (VWAP rises). Third bar plunges → close
    # well below the running VWAP, triggering vwap_break.
    fake_bars = [
        {"timestamp": base, "open": 100, "high": 102, "low": 99, "close": 101},
        {"timestamp": base + timedelta(minutes=30), "open": 101, "high": 103, "low": 100, "close": 102},
        {"timestamp": base + timedelta(hours=1), "open": 102, "high": 102, "low": 90, "close": 91},
    ]
    with patch.object(brain_linker, "_fetch_bars_after", return_value=fake_bars):
        stats = link_brain_predictions()
    assert stats["invalidations_hit"] >= 1
    with session_scope() as s:
        row = s.get(BrainPrediction, pred_id)
        assert row.invalidation_hit is True
