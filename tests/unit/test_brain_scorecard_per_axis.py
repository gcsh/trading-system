"""MITS Phase 15.E — Per-axis aggregation in build_brain_scorecard."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pytest

from backend.bot.scorecard.brain_scorecard import (
    PER_AXIS_KEYS,
    build_brain_scorecard,
)
from backend.db import init_db, session_scope
from backend.models.brain_prediction import (
    BrainPrediction,
    OUTCOME_LOSS,
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


def _seed(outcome, *, regime=None, technical=None, options=None,
           analog=None, strategy=None):
    with session_scope() as s:
        s.add(BrainPrediction(
            surface="analysis", ticker="AAPL", window="today",
            pattern="bull_flag",
            suggested_action="BUY_CALL", suggested_direction="long_call",
            posterior_at_decision=0.6, sample_size_at_decision=200,
            outcome=outcome,
            resolved_at=datetime.utcnow() - timedelta(minutes=1),
            regime_call_correct=regime,
            technical_call_correct=technical,
            options_call_correct=options,
            analog_call_correct=analog,
            strategy_call_correct=strategy,
        ))


def test_empty_db_returns_zero_for_all_axes(fresh_db):
    card = build_brain_scorecard(window_trades=50)
    pa = card.per_axis_calibration
    for axis in PER_AXIS_KEYS:
        assert axis in pa
        assert pa[axis]["n"] == 0
        assert pa[axis]["predicted_correct_rate"] == 0.0


def test_per_axis_aggregation_mixed_outcomes(fresh_db):
    # 5 rows with regime call recorded — 3 correct, 2 wrong.
    _seed(OUTCOME_WIN, regime=True, technical=True)
    _seed(OUTCOME_WIN, regime=True, technical=True)
    _seed(OUTCOME_LOSS, regime=True, technical=False)
    _seed(OUTCOME_LOSS, regime=False, technical=False)
    _seed(OUTCOME_WIN, regime=False)
    card = build_brain_scorecard(window_trades=50)
    pa = card.per_axis_calibration
    assert pa["regime"]["n"] == 5
    assert abs(pa["regime"]["predicted_correct_rate"] - 0.6) < 1e-6
    # technical has 4 rows recorded (last seed didn't pass technical).
    assert pa["technical"]["n"] == 4
    assert abs(pa["technical"]["predicted_correct_rate"] - 0.5) < 1e-6
    # options/analog/strategy not populated on these rows.
    assert pa["options"]["n"] == 0
    assert pa["analog"]["n"] == 0
    assert pa["strategy"]["n"] == 0


def test_per_axis_surfaces_in_to_dict(fresh_db):
    _seed(OUTCOME_WIN, options=True, analog=True, strategy=True)
    _seed(OUTCOME_LOSS, options=False, analog=False, strategy=False)
    _seed(OUTCOME_WIN, options=True, analog=True, strategy=True)
    card = build_brain_scorecard(window_trades=50)
    d = card.to_dict()
    assert "per_axis_calibration" in d
    pa = d["per_axis_calibration"]
    for axis in ("options", "analog", "strategy"):
        assert pa[axis]["n"] == 3
        assert abs(pa[axis]["predicted_correct_rate"] - (2 / 3)) < 1e-3
