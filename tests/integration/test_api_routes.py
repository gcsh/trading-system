import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(temp_db):
    os.environ["DISABLE_SCHEDULER"] = "1"
    # Re-import main to bind to the per-test DB.
    from importlib import reload

    from backend import main as main_mod

    reload(main_mod)
    return TestClient(main_mod.app)


def test_status_returns_running_false_initially(client):
    r = client.get("/bot/status")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False


def test_start_then_stop(client):
    assert client.post("/bot/start").status_code == 200
    assert client.get("/bot/status").json()["running"] is True
    assert client.post("/bot/stop").status_code == 200
    assert client.get("/bot/status").json()["running"] is False


def test_config_round_trip(client):
    initial = client.get("/config").json()
    initial["risk"]["max_position_size_usd"] = 777
    saved = client.post("/config", json=initial).json()
    assert saved["risk"]["max_position_size_usd"] == 777
    assert client.get("/config").json()["risk"]["max_position_size_usd"] == 777


def test_trades_endpoints(client):
    assert client.get("/trades/list").status_code == 200
    summary = client.get("/trades/summary").json()
    assert "trade_count" in summary
