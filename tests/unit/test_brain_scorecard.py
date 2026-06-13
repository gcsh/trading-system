"""MITS Phase 14.D — Brain scorecard tests."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pytest

from backend.bot.scorecard.brain_scorecard import (
    CALIBRATION_BIN_COUNT,
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


def _seed(posterior, outcome, *, ticker="AAPL", invalidation_hit=None,
          invalidation_saved=None, surface="analysis"):
    with session_scope() as s:
        s.add(BrainPrediction(
            surface=surface,
            ticker=ticker,
            window="today",
            pattern="bull_flag",
            suggested_action="BUY_CALL" if outcome != OUTCOME_LOSS else "BUY_CALL",
            suggested_direction="long_call",
            posterior_at_decision=posterior,
            sample_size_at_decision=200,
            confidence_self_assessment=posterior,
            invalidation_hit=invalidation_hit,
            invalidation_saved_capital=invalidation_saved,
            outcome=outcome,
            resolved_at=datetime.utcnow() - timedelta(minutes=1),
        ))


def test_empty_db_returns_zero_window(fresh_db):
    card = build_brain_scorecard(window_trades=50)
    assert card.window_trades == 0
    assert card.predicted_win_rate == 0.0
    assert card.realized_win_rate == 0.0
    assert card.calibration_gap_pp == 0.0
    assert len(card.calibration_bins) == CALIBRATION_BIN_COUNT


def test_calibration_bins_math(fresh_db):
    # Bin centred at 0.55 (idx 5): three predictions, posterior 0.55,
    # two win + one loss → realized = 0.667.
    _seed(0.55, OUTCOME_WIN)
    _seed(0.55, OUTCOME_WIN)
    _seed(0.55, OUTCOME_LOSS)
    # Bin centred at 0.85 (idx 8): one prediction, posterior 0.85, win.
    _seed(0.85, OUTCOME_WIN)

    card = build_brain_scorecard(window_trades=50)
    assert card.window_trades == 4
    # Predicted = (0.55 * 3 + 0.85) / 4 = 0.625
    assert abs(card.predicted_win_rate - 0.625) < 1e-6
    # Realized = 3 / 4
    assert abs(card.realized_win_rate - 0.75) < 1e-6
    # Calibration gap (predicted - realized) in pp = -12.5
    assert abs(card.calibration_gap_pp - (-12.5)) < 1e-6
    by_mid = {round(b["bin_midpoint"], 2): b for b in card.calibration_bins}
    bin_55 = by_mid[0.55]
    assert bin_55["n"] == 3
    assert abs(bin_55["realized_win_rate"] - (2 / 3)) < 1e-3
    bin_85 = by_mid[0.85]
    assert bin_85["n"] == 1
    assert bin_85["realized_win_rate"] == 1.0


def test_invalidation_hit_rate(fresh_db):
    _seed(0.6, OUTCOME_WIN, invalidation_hit=True, invalidation_saved=True)
    _seed(0.6, OUTCOME_LOSS, invalidation_hit=True, invalidation_saved=False)
    _seed(0.6, OUTCOME_WIN, invalidation_hit=False)
    _seed(0.6, OUTCOME_WIN, invalidation_hit=False)
    card = build_brain_scorecard(window_trades=50)
    assert abs(card.invalidation_hit_rate - 0.5) < 1e-6
    assert abs(card.invalidation_saved_capital_rate - 0.5) < 1e-6


def test_surface_filter(fresh_db):
    _seed(0.7, OUTCOME_WIN, surface="analysis")
    _seed(0.7, OUTCOME_LOSS, surface="eod_analysis")
    card = build_brain_scorecard(surface="analysis", window_trades=50)
    assert card.window_trades == 1
    assert card.realized_win_rate == 1.0
