"""MITS Phase 7.6 — /regime/intraday endpoint tests."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import regime as regime_routes


def _build_app(engine=None) -> FastAPI:
    app = FastAPI()
    app.include_router(regime_routes.router)
    app.state.engine = engine
    return app


def test_default_state_when_no_events_yet(temp_db):
    """Cold DB → endpoint returns the default normal state without
    erroring."""
    client = TestClient(_build_app(engine=None))
    resp = client.get("/regime/intraday")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "normal"
    assert body["mode"] == "statistical"


def test_shape_includes_all_required_fields(temp_db):
    client = TestClient(_build_app(engine=None))
    body = client.get("/regime/intraday").json()
    expected = {
        "state", "severity", "since", "vix", "vix_change_pct",
        "breadth", "put_call", "mode", "last_scan_at",
        "current_hypothesis",
    }
    assert expected <= set(body.keys())


def test_state_from_persisted_event(temp_db):
    """When a panic event was persisted, the endpoint surfaces it."""
    from backend.db import session_scope
    from backend.models.intraday_regime_event import IntradayRegimeEvent
    with session_scope() as s:
        s.add(IntradayRegimeEvent(
            prior_state="normal", new_state="panic",
            severity="high", spy_pct_change_30m=-2.0, vix_spot=30.0,
            breadth_ratio=0.2, put_call_ratio=1.4,
        ))
        s.commit()
    client = TestClient(_build_app(engine=None))
    body = client.get("/regime/intraday").json()
    assert body["state"] == "panic"
    assert body["mode"] == "opportunistic"
    assert body["vix"] == 30.0


def test_recent_events_endpoint_returns_persisted_rows(temp_db):
    from backend.db import session_scope
    from backend.models.intraday_regime_event import IntradayRegimeEvent
    with session_scope() as s:
        s.add(IntradayRegimeEvent(
            prior_state="normal", new_state="panic", severity="high"))
        s.add(IntradayRegimeEvent(
            prior_state="panic", new_state="squeeze", severity="high"))
        s.commit()
    client = TestClient(_build_app(engine=None))
    body = client.get("/regime/events?limit=10").json()
    assert "events" in body
    assert len(body["events"]) == 2
    # Newest first.
    assert body["events"][0]["new_state"] == "squeeze"
