"""Heatseeker / Flowseeker API + WebSocket — mocked signals, no network."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(temp_db, monkeypatch):
    from backend.api.routes import flowseeker as fs_route
    from backend.api.routes import heatseeker as hs_route
    from backend.bot.signals import flow as flow_mod
    from backend.bot.signals.flow import FlowAlert
    from backend.bot.signals.gex import GEXResult

    fake_gex = GEXResult(
        ticker="SPY", timestamp="t", spot_price=500.0, call_wall=510.0, put_wall=490.0,
        gamma_flip=500.0, dealer_regime="long_gamma",
        gex_by_strike=[{"strike": 500.0, "call_gex": 1.0, "put_gex": -1.0, "net_gex": 0.0}],
        source="test", ok=True,
    )
    fake_alert = FlowAlert(ticker="SPY", timestamp="t", strike=500.0, expiry="2026-06-19",
                           premium=120_000.0, option_type="call", trade_type="sweep",
                           sentiment="bullish", size=200, urgency_score=0.9)

    monkeypatch.setattr(hs_route, "gex", lambda t: fake_gex)
    monkeypatch.setattr(fs_route, "live_flow", lambda tickers, limit=50: [fake_alert])
    monkeypatch.setattr(fs_route, "flow_for", lambda t: [fake_alert])
    monkeypatch.setattr(fs_route, "summary", lambda alerts: {"count": len(alerts), "net_sentiment": "bullish"})
    monkeypatch.setattr(fs_route, "darkpool", lambda: [])
    # /ws/flow imports live_flow from the source module at call time.
    monkeypatch.setattr(flow_mod, "live_flow", lambda tickers, limit=25: [fake_alert])

    from importlib import reload

    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


def test_heatseeker_api_endpoint(client):
    r = client.get("/heatseeker/SPY")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "SPY"
    assert body["dealer_regime"] == "long_gamma"
    assert body["call_wall"] == 510.0 and body["put_wall"] == 490.0
    assert isinstance(body["gex_by_strike"], list) and body["gex_by_strike"]

    regime = client.get("/heatseeker/regime?symbol=SPY").json()
    assert regime["dealer_regime"] == "long_gamma"


def test_flowseeker_api_endpoint(client):
    live = client.get("/flow/live").json()
    assert isinstance(live, list) and live[0]["ticker"] == "SPY"
    assert live[0]["urgency_score"] == 0.9

    one = client.get("/flow/SPY").json()
    assert one[0]["sentiment"] == "bullish"

    summary = client.get("/flow/summary").json()
    assert summary["count"] == 1


def test_flow_websocket(client):
    with client.websocket_connect("/ws/flow") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "flow"
        assert isinstance(msg["alerts"], list)
        assert msg["alerts"][0]["ticker"] == "SPY"


def test_regime_exposes_opex_and_shift_fields(client):
    body = client.get("/heatseeker/regime?symbol=SPY").json()
    for key in ("opex_day", "opex_week", "stale", "flip_direction",
                "prev_call_wall", "prev_gamma_flip"):
        assert key in body
    assert isinstance(body["opex_day"], bool)
    assert isinstance(body["opex_week"], bool)


def test_regime_history_route(client):
    # Empty to start.
    empty = client.get("/heatseeker/regime/history?symbol=SPY").json()
    assert empty["ticker"] == "SPY" and empty["history"] == []

    # Insert a snapshot directly, then it should come back through the route.
    from backend.db import session_scope
    from backend.models.gex_history import GexRegimeHistory

    with session_scope() as session:
        session.add(GexRegimeHistory(
            ticker="SPY", spot_price=500.0, call_wall=510.0, put_wall=490.0,
            gamma_flip=500.0, dealer_regime="long_gamma",
        ))

    body = client.get("/heatseeker/regime/history?symbol=SPY").json()
    assert len(body["history"]) == 1
    assert body["history"][0]["dealer_regime"] == "long_gamma"
    assert body["history"][0]["spot_price"] == 500.0


def test_websocket_dedup_no_replay(client):
    # First connection receives the alert; reconnect must not replay it (#3).
    with client.websocket_connect("/ws/flow") as ws:
        first = ws.receive_json()
        assert first["alerts"] and first["alerts"][0]["ticker"] == "SPY"
    with client.websocket_connect("/ws/flow") as ws:
        second = ws.receive_json()
        assert second["alerts"] == []   # already-seen alert is not re-pushed
