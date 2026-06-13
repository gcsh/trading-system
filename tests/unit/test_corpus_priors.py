"""MITS Phase 0 — pattern priors loader test."""
from __future__ import annotations

import os
import tempfile

import pytest

from backend.bot.corpus.priors_loader import (
    DEFAULT_PRIORS, load_default_priors,
)
from backend.db import init_db, session_scope
from backend.models.pattern_prior import PatternPrior


pytestmark = [pytest.mark.unit, pytest.mark.invariant]


@pytest.fixture
def fresh_db():
    """Spin up an isolated SQLite DB for the test, restore the global
    engine to its previous state on teardown."""
    import backend.db as db_mod
    prev_engine = db_mod._engine
    prev_session = db_mod._SessionLocal
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db(path)
    try:
        yield path
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


def test_loads_all_default_priors(fresh_db):
    stats = load_default_priors()
    assert stats["inserted"] == len(DEFAULT_PRIORS)
    assert stats["updated"] == 0
    assert stats["errors"] == 0
    with session_scope() as s:
        rows = s.query(PatternPrior).all()
        assert len(rows) == len(DEFAULT_PRIORS)


def test_idempotent_on_rerun(fresh_db):
    load_default_priors()
    stats = load_default_priors()
    assert stats["inserted"] == 0
    assert stats["updated"] == len(DEFAULT_PRIORS)


def test_priors_have_required_fields(fresh_db):
    load_default_priors()
    with session_scope() as s:
        for row in s.query(PatternPrior).all():
            assert row.pattern
            assert row.cohort_descriptor
            assert 0.0 <= float(row.prior_win_rate) <= 1.0
            assert int(row.prior_weight) >= 1
            assert row.source
