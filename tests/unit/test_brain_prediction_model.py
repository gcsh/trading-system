"""MITS Phase 14.D — BrainPrediction ORM smoke test."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime

import pytest
from sqlalchemy import select

from backend.db import init_db, session_scope
from backend.models.brain_prediction import (
    BrainPrediction,
    OUTCOME_PENDING,
    OUTCOME_WIN,
)


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


def test_brain_prediction_columns_and_to_dict(fresh_db):
    with session_scope() as s:
        s.add(BrainPrediction(
            surface="analysis",
            ticker="NVDA",
            window="today",
            pattern="bull_flag",
            suggested_action="BUY_CALL",
            suggested_direction="long_call",
            suggested_strike=120.0,
            suggested_dte=30,
            posterior_at_decision=0.71,
            sample_size_at_decision=412,
            confidence_self_assessment=0.65,
            invalidation_json=json.dumps(["below VWAP", "below 50EMA"]),
            thesis_paragraph="Bull flag in a trending-up regime…",
        ))
    with session_scope() as s:
        row = s.execute(select(BrainPrediction)).scalars().first()
        assert row is not None
        d = row.to_dict()
    expected_keys = {
        "id", "surface", "ticker", "window", "pattern",
        "suggested_action", "suggested_direction", "suggested_strike",
        "suggested_dte", "posterior_at_decision",
        "sample_size_at_decision", "confidence_self_assessment",
        "invalidation_json", "thesis_paragraph", "created_at",
        "linked_trade_id", "actual_pnl_pct", "invalidation_hit",
        "invalidation_saved_capital", "outcome", "resolved_at",
    }
    assert expected_keys.issubset(set(d.keys()))
    assert d["surface"] == "analysis"
    assert d["ticker"] == "NVDA"
    assert d["suggested_action"] == "BUY_CALL"
    assert d["outcome"] == OUTCOME_PENDING
    assert d["linked_trade_id"] is None
    assert d["invalidation_hit"] is None


def test_brain_prediction_resolved_to_dict_shape(fresh_db):
    with session_scope() as s:
        s.add(BrainPrediction(
            surface="eod_analysis",
            ticker="SPY",
            pattern="bull_flag",
            suggested_action="BUY_CALL",
            suggested_direction="long_call",
            posterior_at_decision=0.68,
            sample_size_at_decision=200,
            outcome=OUTCOME_WIN,
            actual_pnl_pct=0.12,
            invalidation_hit=False,
            resolved_at=datetime.utcnow(),
        ))
    with session_scope() as s:
        row = s.execute(select(BrainPrediction)).scalars().first()
        d = row.to_dict()
        assert d["outcome"] == OUTCOME_WIN
        assert d["actual_pnl_pct"] == 0.12
        assert d["invalidation_hit"] is False
        assert d["resolved_at"] is not None
