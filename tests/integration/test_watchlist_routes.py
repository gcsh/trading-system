"""Watchlist CRUD + alerts route integration."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(temp_db, monkeypatch):
    # Disable quote enrichment so we don't hit yfinance during tests.
    from backend.api.routes import watchlist as wl_route

    monkeypatch.setattr(wl_route, "_yf_quote", lambda ticker: None)
    monkeypatch.setattr(wl_route._finnhub_client, "api_key", "")

    from importlib import reload

    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


def test_add_list_delete(client):
    add = client.post("/watchlist", json={"ticker": "spy"}).json()
    assert add["ticker"] == "SPY"

    items = client.get("/watchlist/items").json()
    assert any(i["ticker"] == "SPY" for i in items)

    item_id = items[0]["id"]
    delete = client.delete(f"/watchlist/{item_id}").json()
    assert delete["deleted"] == item_id

    final = client.get("/watchlist/items").json()
    assert all(i["ticker"] != "SPY" for i in final)


def test_add_dedupes(client):
    a = client.post("/watchlist", json={"ticker": "AAPL"}).json()
    b = client.post("/watchlist", json={"ticker": "AAPL"}).json()
    assert a["id"] == b["id"]


def test_add_missing_ticker_400(client):
    r = client.post("/watchlist", json={})
    assert r.status_code == 400


def test_delete_missing_404(client):
    r = client.delete("/watchlist/99999")
    assert r.status_code == 404


def test_alerts_endpoint_returns_list(client):
    r = client.get("/alerts/list")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
