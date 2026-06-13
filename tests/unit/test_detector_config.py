"""MITS Phase 3 — DetectorConfig model tests."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from backend.db import init_db, session_scope
from backend.models.detector_config import DetectorConfig


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


def test_detector_config_defaults(fresh_db):
    """A new row defaults to enabled=True, params_json='{}', source='builtin'."""
    with session_scope() as s:
        row = DetectorConfig(name="bull_flag")
        s.add(row)
        s.flush()
        assert row.enabled is True
        assert row.params_json == "{}"
        assert row.source == "builtin"
        assert row.pine_source is None
        assert row.last_updated_at is not None


def test_detector_config_toggle_persists(fresh_db):
    """Toggling enabled false persists across session boundaries."""
    with session_scope() as s:
        s.add(DetectorConfig(name="breakout", enabled=False))
    with session_scope() as s2:
        row = s2.query(DetectorConfig).filter_by(name="breakout").one()
        assert row.enabled is False


def test_detector_config_params_round_trip(fresh_db):
    """params_json round-trips through to_dict."""
    with session_scope() as s:
        s.add(DetectorConfig(
            name="breakout",
            params_json=json.dumps({"lookback_bars": 50,
                                          "min_breakout_pct": 0.01}),
        ))
    with session_scope() as s2:
        row = s2.query(DetectorConfig).filter_by(name="breakout").one()
        d = row.to_dict()
        assert d["params"]["lookback_bars"] == 50
        assert d["params"]["min_breakout_pct"] == 0.01


def test_detector_config_pine_source(fresh_db):
    """Pine-imported rows carry source and pine_source columns."""
    with session_scope() as s:
        s.add(DetectorConfig(
            name="my_pine_macd", source="pine_import",
            pine_source="// macd cross\nta.crossover(macd, signal)",
        ))
    with session_scope() as s2:
        row = s2.query(DetectorConfig).filter_by(name="my_pine_macd").one()
        d = row.to_dict()
        assert d["source"] == "pine_import"
        assert "macd" in d["pine_source"]


def test_detector_config_to_dict_handles_bad_json(fresh_db):
    """A malformed params_json returns an empty dict (no crash)."""
    with session_scope() as s:
        s.add(DetectorConfig(name="bad", params_json="not json {}"))
    with session_scope() as s2:
        row = s2.query(DetectorConfig).filter_by(name="bad").one()
        d = row.to_dict()
        assert d["params"] == {}


def test_detector_config_unique_on_name(fresh_db):
    """Inserting two rows for the same name should violate the uniqueness."""
    from sqlalchemy.exc import IntegrityError
    with session_scope() as s:
        s.add(DetectorConfig(name="dup"))
    with pytest.raises(IntegrityError):
        with session_scope() as s2:
            s2.add(DetectorConfig(name="dup"))
