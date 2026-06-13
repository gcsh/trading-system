"""MITS Phase 3 — /detectors API tests."""
from __future__ import annotations

import json
import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import detectors as detectors_routes
from backend.bot.detectors import (
    DETECTOR_REGISTRY, clear_detector_config_cache,
)
from backend.db import init_db, session_scope
from backend.models.detector_config import DetectorConfig


pytestmark = [pytest.mark.unit]


@pytest.fixture
def client():
    import backend.db as db_mod
    prev_engine = db_mod._engine
    prev_session = db_mod._SessionLocal
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db(path)
    clear_detector_config_cache()
    app = FastAPI()
    app.include_router(detectors_routes.router)
    try:
        yield TestClient(app)
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass
        clear_detector_config_cache()


def test_list_detectors_returns_registry(client):
    r = client.get("/detectors")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) >= len(DETECTOR_REGISTRY)
    names = {row["name"] for row in rows}
    # Sanity-check a known detector is in the response.
    assert "bull_flag" in names
    # Every row exposes family + enabled.
    for row in rows:
        assert "family" in row
        assert "enabled" in row
        assert "default_params" in row


def test_patch_toggle_disables_detector(client):
    r = client.patch("/detectors/bull_flag",
                          json={"enabled": False})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    # Persisted.
    with session_scope() as s:
        row = s.query(DetectorConfig).filter_by(name="bull_flag").one()
        assert row.enabled is False


def test_patch_params_persists(client):
    r = client.patch("/detectors/bull_flag",
                          json={"params": {"min_thrust_pct": 0.08}})
    assert r.status_code == 200
    body = r.json()
    assert body["params"]["min_thrust_pct"] == 0.08


def test_patch_unknown_detector_404(client):
    r = client.patch("/detectors/_no_such_detector_xyz",
                          json={"enabled": False})
    assert r.status_code == 404


def test_pine_import_persists_row(client):
    # Use a source the translator's regex can actually match.
    pine = (
        "//@version=5\n"
        "strategy('My MACD', overlay=true)\n"
        "longCond = ta.crossover(macd, signal)\n"
        "shortCond = rsi < 30\n"
    )
    r = client.post("/detectors/import-pine",
                          json={"name": "my_macd_test", "source": pine})
    assert r.status_code == 200
    body = r.json()
    assert body["row"]["name"] == "my_macd_test"
    assert body["row"]["source"] == "pine_import"
    # Recognized rule made it through. The translator picks up the
    # MACD crossover + the RSI threshold.
    recognized_text = " ".join(body.get("recognized", []))
    assert "MACD" in recognized_text or "RSI" in recognized_text


def test_pine_import_400_on_empty_source(client):
    r = client.post("/detectors/import-pine",
                          json={"name": "empty", "source": ""})
    assert r.status_code == 400


def test_pine_import_rejects_whitespace_name(client):
    r = client.post("/detectors/import-pine",
                          json={"name": "with space", "source": "rsi < 30"})
    assert r.status_code == 400


def test_listing_reflects_patch(client):
    # Disable one, then verify GET shows it disabled.
    client.patch("/detectors/breakout", json={"enabled": False})
    r = client.get("/detectors")
    rows = r.json()
    row = next(r for r in rows if r["name"] == "breakout")
    assert row["enabled"] is False
