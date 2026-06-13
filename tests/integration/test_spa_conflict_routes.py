"""SPA-vs-API conflict-route fallback.

Five frontend SPA paths (``/tomorrow``, ``/analysis/{ticker}``,
``/detectors``, ``/trial-scorecard``, ``/retrospective``) share their
URL with a backend JSON router of the same prefix. Prior to the
``spa_fallback_for_browsers`` middleware, a direct browser visit
returned raw JSON because routers are registered before the SPA
catch-all.

These tests pin the fix:

* GET with ``Accept: text/html`` (browser) returns the SPA shell.
* GET with ``Accept: application/json`` (the React fetch client)
  still returns the original JSON payload.
* Other paths (``/bot/status``) keep their pre-existing behavior
  regardless of Accept header — the middleware list is
  surgically scoped to the 5 conflict prefixes.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


_DIST_INDEX = (
    Path(__file__).resolve().parents[2] / "frontend" / "dist" / "index.html"
)
_REQUIRES_DIST = pytest.mark.skipif(
    not _DIST_INDEX.exists(),
    reason="frontend/dist/index.html is missing — build the UI first",
)


@pytest.fixture()
def client(temp_db):
    """Boot a fresh app against a per-test SQLite DB."""
    os.environ["DISABLE_SCHEDULER"] = "1"
    from importlib import reload

    from backend import main as main_mod

    reload(main_mod)
    return TestClient(main_mod.app)


# ---------------------------------------------------------------------------
# Browser deep-links to conflict paths must return the SPA shell.
# ---------------------------------------------------------------------------


@_REQUIRES_DIST
def test_tomorrow_browser_returns_html(client):
    r = client.get("/tomorrow", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # SPA shell — the React app bootstraps from #root.
    assert "<div id=\"root\">" in r.text or "id=root" in r.text


@_REQUIRES_DIST
def test_analysis_ticker_browser_returns_html(client):
    r = client.get("/analysis/AAPL", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


@_REQUIRES_DIST
def test_detectors_browser_returns_html(client):
    r = client.get("/detectors", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


@_REQUIRES_DIST
def test_trial_scorecard_browser_returns_html(client):
    r = client.get("/trial-scorecard", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


@_REQUIRES_DIST
def test_retrospective_browser_returns_html(client):
    r = client.get("/retrospective", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


@_REQUIRES_DIST
def test_root_returns_html(client):
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# JSON clients (the React fetch layer) keep receiving JSON.
# ---------------------------------------------------------------------------


def test_tomorrow_json_returns_api_payload(client):
    r = client.get("/tomorrow", headers={"Accept": "application/json"})
    assert r.status_code == 200
    body = r.json()
    # tomorrow.list_tomorrow returns a dict with a ``rows`` key (possibly
    # empty when the EOD pass hasn't run for today). Either shape proves
    # the API — not the SPA — answered.
    assert isinstance(body, dict)
    assert "rows" in body or "date" in body


def test_analysis_ticker_json_returns_api_payload(client):
    # Use an obviously-fake ticker so the deep composer short-circuits
    # quickly. The shape is what we're checking, not the content.
    r = client.get(
        "/analysis/AAPL",
        headers={"Accept": "application/json"},
    )
    # Whether AAPL has data or not, the API must answer with JSON (not
    # the SPA HTML). Status may be 200, 404, or 5xx depending on data
    # availability; the critical assertion is content-type.
    assert "application/json" in r.headers.get("content-type", "")


def test_detectors_json_returns_api_payload(client):
    r = client.get("/detectors", headers={"Accept": "application/json"})
    assert r.status_code == 200
    assert "application/json" in r.headers["content-type"]


def test_trial_scorecard_json_returns_api_payload(client):
    r = client.get(
        "/trial-scorecard", headers={"Accept": "application/json"}
    )
    # Trial scorecard returns JSON even when no trial is active; assert
    # the API answered (not the SPA fallback).
    assert "application/json" in r.headers.get("content-type", "")


def test_retrospective_json_returns_api_payload(client):
    r = client.get(
        "/retrospective", headers={"Accept": "application/json"}
    )
    assert "application/json" in r.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Non-conflict paths are untouched by the middleware.
# ---------------------------------------------------------------------------


def test_bot_status_browser_still_returns_json(client):
    """``/bot/status`` is NOT in SPA_CONFLICT_PREFIXES, so even a
    browser-style Accept header still gets the JSON payload — proves the
    middleware is surgically scoped."""
    r = client.get("/bot/status", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "application/json" in r.headers.get("content-type", "")
    assert "running" in r.json()


def test_bot_status_json_returns_json(client):
    r = client.get("/bot/status", headers={"Accept": "application/json"})
    assert r.status_code == 200
    assert "application/json" in r.headers["content-type"]


# ---------------------------------------------------------------------------
# Mutating verbs on conflict prefixes must NOT be SPA-fallback'd.
# ---------------------------------------------------------------------------


def test_post_tomorrow_rebuild_still_hits_api(client):
    """The middleware only intercepts GET. POST /tomorrow/rebuild must
    still reach the API (it runs the EOD pass)."""
    r = client.post(
        "/tomorrow/rebuild", headers={"Accept": "text/html"}
    )
    # Whatever the EOD endpoint returns (success or error), it MUST be
    # JSON-shaped — not the SPA HTML.
    assert "application/json" in r.headers.get("content-type", "")
