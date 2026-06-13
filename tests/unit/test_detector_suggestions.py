"""MITS Phase 6 (P6.3) — Self-disabling detector suggestion tests."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import detector_scorecard as ds_routes
from backend.bot.scorecard.suggestions import run_suggestions_pass
from backend.db import init_db, session_scope
from backend.models.detector_config import DetectorConfig
from backend.models.detector_suggestion import (
    DetectorSuggestion,
    REASON_LOW_POSTERIOR,
    REASON_RECOVERED_POSTERIOR,
    SUGGESTION_STATUS_DISMISSED,
    SUGGESTION_STATUS_PENDING,
)
from backend.models.knowledge_graph_cell import KnowledgeGraphCell


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
        yield path
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.fixture
def client(fresh_db):
    app = FastAPI()
    app.include_router(ds_routes.router)
    return TestClient(app)


def _seed_cell(detector: str, posterior: float, n: int,
                  *, split: str = "out_of_sample") -> None:
    with session_scope() as s:
        cell = KnowledgeGraphCell(
            ticker="ANY", pattern=detector, regime="trending_up",
            vol_state="normal", time_bucket="rth", horizon="1d",
            sample_split=split,
            sample_size=n, win_rate=posterior,
            posterior_win_rate=posterior,
        )
        s.add(cell)


def _set_detector_enabled(name: str, enabled: bool) -> None:
    with session_scope() as s:
        row = s.query(DetectorConfig).filter_by(name=name).first()
        if row is None:
            row = DetectorConfig(name=name, enabled=enabled,
                                            params_json="{}",
                                            source="builtin")
            s.add(row)
        else:
            row.enabled = enabled


def test_low_posterior_triggers_suggestion(fresh_db):
    _seed_cell("bull_flag", posterior=0.40, n=120)
    stats = run_suggestions_pass()
    assert stats["low_posterior_suggested"] >= 1
    with session_scope() as s:
        rows = s.query(DetectorSuggestion).filter_by(
            detector_name="bull_flag",
            reason=REASON_LOW_POSTERIOR,
        ).all()
        assert len(rows) == 1
        assert rows[0].status == SUGGESTION_STATUS_PENDING


def test_idempotent_no_duplicate_suggestion(fresh_db):
    _seed_cell("bull_flag", posterior=0.40, n=120)
    run_suggestions_pass()
    run_suggestions_pass()
    with session_scope() as s:
        rows = s.query(DetectorSuggestion).filter_by(
            detector_name="bull_flag",
            reason=REASON_LOW_POSTERIOR,
        ).all()
        assert len(rows) == 1


def test_below_min_n_does_not_trigger(fresh_db):
    _seed_cell("bull_flag", posterior=0.30, n=20)
    stats = run_suggestions_pass()
    assert stats["low_posterior_suggested"] == 0


def test_accept_disables_detector(client, fresh_db):
    _seed_cell("bull_flag", posterior=0.40, n=120)
    run_suggestions_pass()
    with session_scope() as s:
        sugg = s.query(DetectorSuggestion).filter_by(
            detector_name="bull_flag").first()
        sugg_id = sugg.id
    r = client.post(f"/detector-suggestions/{sugg_id}/accept")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "accepted"
    assert body["new_enabled"] is False
    # Persisted on DetectorConfig.
    with session_scope() as s:
        cfg = s.query(DetectorConfig).filter_by(name="bull_flag").first()
        assert cfg is not None and cfg.enabled is False


def test_dismiss_blocks_new_suggestion_within_cooldown(client, fresh_db):
    _seed_cell("bull_flag", posterior=0.40, n=120)
    run_suggestions_pass()
    with session_scope() as s:
        sugg = s.query(DetectorSuggestion).filter_by(
            detector_name="bull_flag").first()
        sugg_id = sugg.id
    r = client.post(f"/detector-suggestions/{sugg_id}/dismiss")
    assert r.status_code == 200
    assert r.json()["status"] == "dismissed"
    # Running the pass again should NOT create a new suggestion
    # because we're inside the cooldown window.
    stats2 = run_suggestions_pass()
    assert stats2["low_posterior_skipped_cooldown"] >= 1
    with session_scope() as s:
        n = s.query(DetectorSuggestion).filter_by(
            detector_name="bull_flag",
            reason=REASON_LOW_POSTERIOR,
        ).count()
        assert n == 1  # only the dismissed one


def test_recovered_posterior_suggests_reenable(fresh_db):
    # Detector currently disabled but performing well in live data.
    _set_detector_enabled("morning_star", enabled=False)
    _seed_cell("morning_star", posterior=0.70, n=50)
    stats = run_suggestions_pass()
    assert stats["recovered_suggested"] >= 1
    with session_scope() as s:
        rows = s.query(DetectorSuggestion).filter_by(
            detector_name="morning_star",
            reason=REASON_RECOVERED_POSTERIOR,
        ).all()
        assert len(rows) == 1


def test_list_pending_route(client, fresh_db):
    _seed_cell("bull_flag", posterior=0.40, n=120)
    run_suggestions_pass()
    r = client.get("/detector-suggestions?status=pending")
    assert r.status_code == 200
    arr = r.json()
    assert any(d["detector_name"] == "bull_flag" for d in arr)


def test_accept_404_when_missing(client):
    r = client.post("/detector-suggestions/999999/accept")
    assert r.status_code == 404
