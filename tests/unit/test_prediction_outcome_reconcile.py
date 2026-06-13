"""MITS Phase 5 (P5.2) — prediction→outcome reconcile tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from backend.bot.eod_bias import reconcile_outcomes
from backend.db import init_db, session_scope
from backend.models.decision_log import DecisionLog
from backend.models.eod_analysis import EodAnalysis
from backend.models.eod_prediction_outcome import (
    EodPredictionOutcome, OUTCOME_NOT_TRADED, OUTCOME_PENDING,
    OUTCOME_TRADED_DIVERGED, OUTCOME_TRADED_MATCHED,
)
from backend.models.trade import Trade


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db_file = tmp_path / "reconcile_test.db"
    monkeypatch.setattr(
        "backend.config.SETTINGS.db_path", str(db_file),
    )
    import backend.db as db_mod
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db(str(db_file))
    yield
    db_mod._engine = None
    db_mod._SessionLocal = None


def _seed_analysis(session, ticker, suggested=None):
    today = datetime.utcnow().date()
    row = EodAnalysis(
        ticker=ticker, analysis_date=today,
        patterns_fired=json.dumps(["bull_flag"]),
        top_pattern="bull_flag",
        top_posterior=0.80, top_sample_size=120,
        rank_score=5.0,
        suggested_action_json=json.dumps(suggested) if suggested else None,
        headline=f"{ticker} headline",
    )
    session.add(row)
    session.flush()
    return row


def _seed_trade(session, ticker, action="BUY_CALL", instrument="option",
                  pnl=None, status="closed", strike=None, price=10.0):
    t = Trade(
        ticker=ticker, action=action,
        quantity=1.0, price=price,
        strategy="test", signal_source="eod_bias",
        confidence=0.7, reason="test",
        paper=1, pnl=pnl, status=status,
        instrument=instrument, option_type=(
            "call" if "CALL" in action else
            "put" if "PUT" in action else None
        ),
        strike=strike,
        timestamp=datetime.utcnow(),
    )
    session.add(t)
    session.flush()
    return t


def test_matched_outcome_when_trade_direction_matches(fresh_db):
    with session_scope() as s:
        _seed_analysis(s, "AAPL", suggested={
            "action": "BUY_CALL", "direction": "long_call",
            "strike": 200.0, "dte": 30,
        })
        _seed_trade(s, "AAPL", action="BUY_CALL", pnl=120.0, status="closed",
                       strike=200.0)
    stats = reconcile_outcomes()
    assert stats["traded_matched"] == 1
    assert stats["traded_diverged"] == 0
    assert stats["not_traded"] == 0
    with session_scope() as s:
        row = s.query(EodPredictionOutcome).filter_by(ticker="AAPL").one()
        assert row.outcome == OUTCOME_TRADED_MATCHED
        assert row.traded == 1
        assert row.actual_pnl_dollars == 120.0


def test_diverged_when_direction_mismatches(fresh_db):
    with session_scope() as s:
        _seed_analysis(s, "TSLA", suggested={
            "action": "BUY_CALL", "direction": "long_call",
            "strike": 250.0, "dte": 30,
        })
        _seed_trade(s, "TSLA", action="BUY_PUT", pnl=-40.0, status="closed",
                       strike=240.0)
    stats = reconcile_outcomes()
    assert stats["traded_diverged"] == 1
    with session_scope() as s:
        row = s.query(EodPredictionOutcome).filter_by(ticker="TSLA").one()
        assert row.outcome == OUTCOME_TRADED_DIVERGED


def test_not_traded_with_skip_reason(fresh_db):
    with session_scope() as s:
        _seed_analysis(s, "NVDA", suggested={
            "action": "BUY_CALL", "direction": "long_call",
            "strike": 110.0, "dte": 30,
        })
        # Decision-log row carries a gate name in status — the reconcile
        # uses it as the skip_reason.
        dl = DecisionLog(
            ticker="NVDA", action="BUY_CALL", strategy="ai_brain",
            confidence=0.8, status="catalyst_gate",
            signal_source="live_engine",
            timestamp=datetime.utcnow(),
        )
        s.add(dl)
    stats = reconcile_outcomes()
    assert stats["not_traded"] == 1
    with session_scope() as s:
        row = s.query(EodPredictionOutcome).filter_by(ticker="NVDA").one()
        assert row.outcome == OUTCOME_NOT_TRADED
        assert row.skip_reason == "catalyst_gate"


def test_pending_when_trade_still_open(fresh_db):
    with session_scope() as s:
        _seed_analysis(s, "AMD", suggested={
            "action": "BUY_CALL", "direction": "long_call",
            "strike": 150.0, "dte": 30,
        })
        _seed_trade(s, "AMD", action="BUY_CALL", pnl=None,
                       status="open", strike=150.0)
    stats = reconcile_outcomes()
    assert stats["pending"] == 1
    with session_scope() as s:
        row = s.query(EodPredictionOutcome).filter_by(ticker="AMD").one()
        assert row.outcome == OUTCOME_PENDING


def test_reconcile_idempotent(fresh_db):
    with session_scope() as s:
        _seed_analysis(s, "AAPL", suggested={
            "action": "BUY_CALL", "direction": "long_call",
            "strike": 200.0, "dte": 30,
        })
        _seed_trade(s, "AAPL", action="BUY_CALL", pnl=50.0, status="closed",
                       strike=200.0)
    reconcile_outcomes()
    reconcile_outcomes()  # second call should NOT duplicate rows.
    with session_scope() as s:
        rows = s.query(EodPredictionOutcome).all()
        assert len(rows) == 1
        assert rows[0].outcome == OUTCOME_TRADED_MATCHED


def test_no_analysis_no_rows(fresh_db):
    stats = reconcile_outcomes()
    assert stats["analysis_rows"] == 0
    assert stats["traded_matched"] == 0
