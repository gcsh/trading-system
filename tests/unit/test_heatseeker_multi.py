"""Phase 19 — multi-expiration GEX heatmap matrix endpoint.

Pins the contract returned by ``GET /heatseeker/multi/{ticker}`` so the
new Dashboard heatmap can stop rendering its EmptyState. The endpoint
delegates to ``gex_by_expiry`` (already battle-tested by Item #14) and
adds two lifts: a per-expiration bucket ``label`` (0DTE/1W/2W/3W/1M/>1M)
and a top-level ``computed_at`` ISO timestamp.

Tests stub ``gex_by_expiry`` so the suite never touches yfinance.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(temp_db, monkeypatch):
    """FastAPI client with ``gex_by_expiry`` swapped for a deterministic
    canned response that covers every bucket boundary."""
    from importlib import reload

    from backend.api.routes import heatseeker as hs_route

    fake_by_expiry = {
        "ticker": "AAPL",
        "spot": 195.5,
        "expiries": [
            {
                "expiry": "2026-06-13", "dte": 0,
                "strikes": [
                    {"strike": 195.0, "call_gex": 12_000.0,
                     "put_gex": -8_000.0, "net_gex": 4_000.0},
                    {"strike": 200.0, "call_gex": 9_000.0,
                     "put_gex": -3_000.0, "net_gex": 6_000.0},
                ],
                "totals": {"call_gex": 21_000.0, "put_gex": -11_000.0,
                            "net_gex": 10_000.0},
            },
            {
                "expiry": "2026-06-20", "dte": 7,
                "strikes": [
                    {"strike": 195.0, "call_gex": 5_500.0,
                     "put_gex": -2_000.0, "net_gex": 3_500.0},
                ],
                "totals": {"call_gex": 5_500.0, "put_gex": -2_000.0,
                            "net_gex": 3_500.0},
            },
            {
                "expiry": "2026-06-27", "dte": 14,
                "strikes": [
                    {"strike": 200.0, "call_gex": 3_000.0,
                     "put_gex": -1_000.0, "net_gex": 2_000.0},
                ],
                "totals": {"call_gex": 3_000.0, "put_gex": -1_000.0,
                            "net_gex": 2_000.0},
            },
            {
                "expiry": "2026-07-04", "dte": 21,
                "strikes": [],
                "totals": {"call_gex": 0.0, "put_gex": 0.0, "net_gex": 0.0},
            },
            {
                "expiry": "2026-07-18", "dte": 35,
                "strikes": [],
                "totals": {"call_gex": 0.0, "put_gex": 0.0, "net_gex": 0.0},
            },
            {
                "expiry": "2026-08-22", "dte": 70,
                "strikes": [],
                "totals": {"call_gex": 100.0, "put_gex": -50.0,
                            "net_gex": 50.0},
            },
        ],
    }
    monkeypatch.setattr(hs_route, "gex_by_expiry",
                        lambda t, max_expiries=12: fake_by_expiry)

    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


def test_multi_endpoint_returns_expirations_array(client):
    """Smoke — 200, top-level shape includes expirations + computed_at."""
    r = client.get("/heatseeker/multi/AAPL")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "AAPL"
    assert body["spot_price"] == pytest.approx(195.5)
    assert isinstance(body["expirations"], list)
    assert len(body["expirations"]) == 6
    assert isinstance(body["computed_at"], str)
    # Each expiration row carries the documented per-expiry schema.
    for row in body["expirations"]:
        for key in ("expiry", "dte", "label",
                    "call_gex_total", "put_gex_total",
                    "net_gex_total", "gex_by_strike"):
            assert key in row, f"row missing {key}"


def test_bucket_labels_cover_full_spec(client):
    """Each DTE bucket boundary lands in the documented label."""
    body = client.get("/heatseeker/multi/AAPL").json()
    by_dte = {row["dte"]: row["label"] for row in body["expirations"]}
    assert by_dte[0] == "0DTE"
    assert by_dte[7] == "1W"
    assert by_dte[14] == "2W"
    assert by_dte[21] == "3W"
    assert by_dte[35] == "1M"
    assert by_dte[70] == ">1M"


def test_per_strike_breakdown_preserves_call_put_net(client):
    """gex_by_strike rows carry (strike, call_gex, put_gex, net_gex)."""
    body = client.get("/heatseeker/multi/AAPL").json()
    zero_dte = next(r for r in body["expirations"] if r["dte"] == 0)
    assert len(zero_dte["gex_by_strike"]) == 2
    by_strike = {s["strike"]: s for s in zero_dte["gex_by_strike"]}
    assert by_strike[195.0]["call_gex"] == pytest.approx(12_000.0)
    assert by_strike[195.0]["put_gex"] == pytest.approx(-8_000.0)
    assert by_strike[195.0]["net_gex"] == pytest.approx(4_000.0)
    assert by_strike[200.0]["net_gex"] == pytest.approx(6_000.0)
    # Totals match the sum of the per-strike rows (allowing rounding).
    sum_net = sum(s["net_gex"] for s in zero_dte["gex_by_strike"])
    assert zero_dte["net_gex_total"] == pytest.approx(sum_net, abs=0.5)


def test_upstream_failure_degrades_to_empty_array(client, monkeypatch):
    """Vendor exception → 200 with empty expirations + ``note`` field —
    never bubbles a 500 to the dashboard."""
    from backend.api.routes import heatseeker as hs_route

    def _raise(t, max_expiries=12):
        raise RuntimeError("ThetaData terminal offline")

    monkeypatch.setattr(hs_route, "gex_by_expiry", _raise)
    r = client.get("/heatseeker/multi/AAPL")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "AAPL"
    assert body["expirations"] == []
    assert "note" in body
    assert "RuntimeError" in body["note"]
    assert "computed_at" in body


def test_max_expiries_query_param_is_passed_through(client, monkeypatch):
    """``max_expiries`` query → forwarded to ``gex_by_expiry`` verbatim."""
    from backend.api.routes import heatseeker as hs_route

    captured = {}

    def _spy(t, max_expiries=12):
        captured["t"] = t
        captured["max_expiries"] = max_expiries
        return {"ticker": t, "spot": 0.0, "expiries": []}

    monkeypatch.setattr(hs_route, "gex_by_expiry", _spy)
    r = client.get("/heatseeker/multi/SPY?max_expiries=5")
    assert r.status_code == 200
    assert captured["t"] == "SPY"
    assert captured["max_expiries"] == 5
