"""Webhook route — secret check, chat_id allowlist, unknown commands."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.bot.notifications.telegram import TelegramNotifier


pytestmark = [pytest.mark.unit]


def _build_notifier(*, allowed_chat="42"):
    session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.text = "ok"
    session.post.return_value = response
    n = TelegramNotifier(
        bot_token="abcd", chat_id=allowed_chat, session=session,
    )
    return n, session


@pytest.fixture()
def client_factory(temp_db, monkeypatch):
    """Builds a test client with a configurable allowlist + secret."""
    def _make(*, secret="super-secret", chat_id="42"):
        monkeypatch.setattr(
            "backend.config.SETTINGS.telegram_webhook_secret", secret,
        )
        monkeypatch.setattr(
            "backend.config.SETTINGS.telegram_chat_id", chat_id,
        )
        from backend.main import create_app
        app = create_app()
        notifier, session = _build_notifier(allowed_chat=chat_id)
        app.state.telegram_notifier = notifier
        return TestClient(app), notifier, session
    return _make


def test_webhook_403_on_bad_secret(client_factory):
    client, _, _ = client_factory(secret="real")
    r = client.post(
        "/telegram/webhook/wrong",
        json={"message": {"chat": {"id": 42}, "text": "/status"}},
    )
    assert r.status_code == 403


def test_webhook_rejects_unknown_chat_id(client_factory):
    client, notifier, session = client_factory(secret="s", chat_id="42")
    r = client.post(
        "/telegram/webhook/s",
        json={"message": {"chat": {"id": 999}, "text": "/status"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "not authorized" in body["reason"]
    # Notifier still posted a "Not authorized" reply to the sender.
    assert session.post.called
    posted_chat = session.post.call_args.kwargs["json"]["chat_id"]
    assert posted_chat == "999"


def test_webhook_dispatches_known_command(client_factory):
    client, notifier, session = client_factory(secret="s", chat_id="42")
    r = client.post(
        "/telegram/webhook/s",
        json={"message": {"chat": {"id": 42}, "text": "/status"}},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # The reply went back to chat_id 42, not the operator default.
    posted = session.post.call_args.kwargs["json"]
    assert posted["chat_id"] == "42"
    assert "<b>Bot status</b>" in posted["text"]


def test_webhook_unknown_command_returns_hint(client_factory):
    client, notifier, session = client_factory(secret="s", chat_id="42")
    r = client.post(
        "/telegram/webhook/s",
        json={"message": {"chat": {"id": 42}, "text": "/nonsense"}},
    )
    assert r.status_code == 200
    posted = session.post.call_args.kwargs["json"]
    assert "Unknown command" in posted["text"]


def test_webhook_handles_missing_text(client_factory):
    client, _, _ = client_factory(secret="s", chat_id="42")
    r = client.post(
        "/telegram/webhook/s",
        json={"message": {"chat": {"id": 42}}},
    )
    assert r.status_code == 200
    assert r.json()["reason"] == "no text"


def test_webhook_handles_malformed_json(client_factory):
    client, _, _ = client_factory(secret="s", chat_id="42")
    r = client.post(
        "/telegram/webhook/s",
        data="not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_webhook_403_when_no_secret_configured(client_factory):
    client, _, _ = client_factory(secret="")
    r = client.post(
        "/telegram/webhook/anything",
        json={"message": {"chat": {"id": 42}, "text": "/status"}},
    )
    assert r.status_code == 403
