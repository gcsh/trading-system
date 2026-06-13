"""Copilot AI routes — runtime API-key entry + chat fallback. No network."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(temp_db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from importlib import reload

    from backend import main as main_mod
    reload(main_mod)
    return TestClient(main_mod.app)


def test_ai_key_roundtrip(client):
    # No key anywhere → unavailable.
    assert client.get("/copilot/ai-status").json()["ai_available"] is False
    # Saving a key via the UI route makes it available at runtime (no restart).
    saved = client.post("/copilot/ai-key", json={"key": "sk-test-123"}).json()
    assert saved["ai_available"] is True
    assert client.get("/copilot/ai-status").json()["ai_available"] is True
    # Briefing reflects the same state.
    assert client.get("/copilot/briefing").json()["ai_available"] is True


def test_ai_key_blank_is_ignored(client):
    client.post("/copilot/ai-key", json={"key": "sk-keep"})
    # A blank submission must not wipe an existing key.
    assert client.post("/copilot/ai-key", json={"key": "  "}).json()["ai_available"] is True


def test_chat_without_key_returns_hint(client):
    body = client.post("/copilot/chat", json={"message": "hello", "history": []}).json()
    assert body["available"] is False
    assert "api key" in body["reply"].lower()


def test_brain_toggle_persists(client):
    out = client.post("/copilot/brain", json={"enabled": True, "web_research": True}).json()
    assert out["brain_enabled"] is True and out["brain_web_research"] is True
    brief = client.get("/copilot/briefing").json()
    assert brief["brain_enabled"] is True and brief["brain_web_research"] is True
