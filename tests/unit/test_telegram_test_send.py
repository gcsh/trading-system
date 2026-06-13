"""/notifications/telegram/test triggers a send via the notifier."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.bot.notifications.telegram import TelegramNotifier


pytestmark = [pytest.mark.unit]


@pytest.fixture()
def app_with_notifier(temp_db):
    from backend.main import create_app
    app = create_app()
    return app


def test_test_route_returns_ok_when_disabled(app_with_notifier):
    client = TestClient(app_with_notifier)
    r = client.post("/notifications/telegram/test")
    assert r.status_code == 200
    body = r.json()
    # Disabled notifier returns True (no-op success).
    assert body["ok"] is True
    assert body["enabled"] is False


def test_test_route_posts_to_telegram_when_enabled(app_with_notifier, monkeypatch):
    session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.text = "ok"
    session.post.return_value = response
    notifier = TelegramNotifier(bot_token="t", chat_id="123", session=session)
    app_with_notifier.state.telegram_notifier = notifier

    client = TestClient(app_with_notifier)
    r = client.post("/notifications/telegram/test")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["enabled"] is True
    # The notifier hit Telegram.
    assert session.post.called
    posted = session.post.call_args.kwargs["json"]
    assert posted["chat_id"] == "123"
    assert "test message" in posted["text"]
    assert posted["parse_mode"] == "HTML"


def test_test_route_returns_503_when_notifier_missing(app_with_notifier):
    app_with_notifier.state.telegram_notifier = None
    client = TestClient(app_with_notifier)
    r = client.post("/notifications/telegram/test")
    assert r.status_code == 503
