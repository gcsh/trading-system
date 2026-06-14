"""Fix N=6 — eod_prediction_outcomes + brain_predictions are now
PAPER_STATE_TABLES, wiped BEFORE trades, so fresh_start no longer
crashes with a FK constraint failure.

The 2026-06-13 incident: fresh_start tried to ``DELETE FROM trades``
while child rows in ``eod_prediction_outcomes`` and
``brain_predictions`` still held FK references to those trades.
SQLite (with FK enforcement on) refused the delete; the operator saw
a reset that "ran but didn't reset."
"""
from __future__ import annotations

from datetime import date as _date, datetime

import pytest

from backend.bot.system_reset import PAPER_STATE_TABLES, fresh_start
from backend.db import session_scope
from backend.models.brain_prediction import BrainPrediction
from backend.models.eod_analysis import EodAnalysis
from backend.models.eod_prediction_outcome import EodPredictionOutcome
from backend.models.trade import Trade


def _seed_eod_analysis_parent(session, analysis_id: int = 1) -> EodAnalysis:
    """EodPredictionOutcome.eod_analysis_id is a FK to eod_analysis.id;
    seed a parent row so the insert validates under PRAGMA foreign_keys=ON."""
    ea = EodAnalysis(
        id=analysis_id, ticker="AAPL",
        analysis_date=_date(2026, 6, 12),
    )
    session.add(ea)
    return ea


def _seed_trade(session, trade_id: int = 42) -> Trade:
    t = Trade(
        id=trade_id, ticker="AAPL", action="BUY_CALL", quantity=1,
        price=100.0, strategy="ai_brain", signal_source="ai_brain",
        confidence=0.9, reason="seed",
        instrument="option", option_type="call",
        strike=100.0, expiration="2030-06-21",
    )
    session.add(t)
    return t


def _seed_brain_prediction(session, trade_id: int = 42) -> BrainPrediction:
    bp = BrainPrediction(
        surface="analysis", ticker="AAPL", suggested_action="BUY_CALL",
        suggested_direction="long_call", suggested_strike=100.0,
        suggested_dte=30, posterior_at_decision=0.6,
        sample_size_at_decision=10,
        linked_trade_id=trade_id, outcome="pending",
        created_at=datetime.utcnow(),
    )
    session.add(bp)
    return bp


def _seed_eod_prediction_outcome(
    session, trade_id: int = 42, eod_analysis_id: int = 1,
) -> EodPredictionOutcome:
    """Caller must also seed an ``eod_analysis`` parent row (via
    ``_seed_eod_analysis_parent``) before insert — the FK is
    INSERT-time enforced under SQLite PRAGMA foreign_keys=ON."""
    eo = EodPredictionOutcome(
        eod_analysis_id=eod_analysis_id, ticker="AAPL",
        analysis_date=_date(2026, 6, 12),
        predicted_direction="long_call", predicted_strike=100.0,
        predicted_dte=30, posterior=0.6, sample_size=10,
        traded=1, trade_id=trade_id, outcome="pending",
    )
    session.add(eo)
    return eo


# ── inventory contract ─────────────────────────────────────────────────


def test_eod_prediction_outcomes_listed_in_paper_state_tables():
    """Lock-in: the table is now in PAPER_STATE_TABLES (was incorrectly
    classified as external-cache before 2026-06-13)."""
    labels = {label for _, label in PAPER_STATE_TABLES}
    assert "eod_prediction_outcomes" in labels


def test_brain_predictions_listed_in_paper_state_tables():
    labels = {label for _, label in PAPER_STATE_TABLES}
    assert "brain_predictions" in labels


def test_child_tables_listed_before_trades():
    """FK constraint: child tables MUST appear before ``trades`` in
    the delete order. Locks in the position so a future refactor
    doesn't re-introduce the FK failure."""
    labels = [label for _, label in PAPER_STATE_TABLES]
    assert labels.index("eod_prediction_outcomes") < labels.index("trades")
    assert labels.index("brain_predictions") < labels.index("trades")


# ── fresh_start with seeded FK rows succeeds ───────────────────────────


def test_fresh_start_with_brain_prediction_referencing_trade(temp_db):
    """Seed a BrainPrediction referencing trade_id=42, seed the trade,
    then call fresh_start. Must not crash."""
    with session_scope() as s:
        _seed_trade(s, 42)
        s.flush()  # parent insert visible before child FK is checked
        _seed_brain_prediction(s, 42)

    report = fresh_start(starting_cash=5000.0)

    assert report.starting_cash == 5000.0
    assert report.account_after["starting_cash"] == 5000.0
    assert report.account_after["cash"] == 5000.0
    with session_scope() as s:
        assert s.query(Trade).count() == 0
        assert s.query(BrainPrediction).count() == 0


def test_fresh_start_with_eod_prediction_outcome_referencing_trade(
    temp_db,
):
    """Seed an EodPredictionOutcome referencing trade_id=42, seed the
    trade, then call fresh_start. Must not crash."""
    with session_scope() as s:
        _seed_eod_analysis_parent(s, 1)
        _seed_trade(s, 42)
        s.flush()  # parents (trade + eod_analysis) visible before child FK
        _seed_eod_prediction_outcome(s, 42)

    report = fresh_start(starting_cash=5000.0)

    assert report.starting_cash == 5000.0
    with session_scope() as s:
        assert s.query(Trade).count() == 0
        assert s.query(EodPredictionOutcome).count() == 0


def test_fresh_start_with_both_child_tables_referencing_trade(temp_db):
    """The full 2026-06-13 scenario: trade row + both child tables
    referencing it. Single fresh_start clears all three without FK
    error."""
    with session_scope() as s:
        _seed_eod_analysis_parent(s, 1)
        _seed_trade(s, 42)
        s.flush()  # commit parents before adding children
        _seed_brain_prediction(s, 42)
        _seed_eod_prediction_outcome(s, 42)
    with session_scope() as s:
        assert s.query(Trade).count() == 1
        assert s.query(BrainPrediction).count() == 1
        assert s.query(EodPredictionOutcome).count() == 1

    report = fresh_start(starting_cash=5000.0)

    assert report.starting_cash == 5000.0
    with session_scope() as s:
        assert s.query(Trade).count() == 0
        assert s.query(BrainPrediction).count() == 0
        assert s.query(EodPredictionOutcome).count() == 0


# ── edge case: child tables empty still works ─────────────────────────


def test_fresh_start_with_empty_child_tables_still_succeeds(temp_db):
    """When the child tables are empty, fresh_start should still
    succeed — the bulk delete on an empty table is a no-op, but the
    code path must not raise."""
    report = fresh_start(starting_cash=5000.0)
    assert report.starting_cash == 5000.0
    assert report.account_after["cash"] == 5000.0
