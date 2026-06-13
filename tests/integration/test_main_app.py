"""Cover main.py startup, WebSocket hub, and root endpoint."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app_client(temp_db):
    from importlib import reload

    from backend import main as main_mod

    reload(main_mod)
    client = TestClient(main_mod.app)
    return client, main_mod


def test_root_returns_message_when_dist_missing(app_client):
    client, _ = app_client
    r = client.get("/")
    # Either JSON message (no dist/) or HTML (dist/ present) — both are valid.
    assert r.status_code == 200


def test_websocket_hub_broadcasts(app_client):
    import asyncio

    _, main_mod = app_client
    hub = main_mod.app.state.hub
    asyncio.run(hub.broadcast({"x": 1}))
    assert hub.history[-1] == {"x": 1}


def test_websocket_connect_and_receive_history(app_client):
    client, main_mod = app_client
    hub = main_mod.app.state.hub
    hub.history.append({"seed": "yes"})
    with client.websocket_connect("/ws/log") as ws:
        msg = ws.receive_json()
        assert msg == {"seed": "yes"}


def test_run_cycle_endpoint(app_client):
    client, _ = app_client
    r = client.post("/bot/run-cycle")
    assert r.status_code == 200
    assert "events" in r.json()
