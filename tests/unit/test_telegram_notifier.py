"""TelegramNotifier — HTTP classification + disabled-no-op."""
from unittest.mock import MagicMock

import pytest

from backend.bot.alerts import Alert
from backend.bot.notifications.filters import (
    TelegramFilterConfig,
    reset_rate_limiter,
)
from backend.bot.notifications.telegram import TelegramNotifier


pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _isolation():
    reset_rate_limiter()
    yield
    reset_rate_limiter()


def _mock_response(status_code: int, body: str = "ok"):
    m = MagicMock()
    m.status_code = status_code
    m.text = body
    return m


def _make_notifier(*, token="abc", chat="42", session=None, filter_cfg=None):
    cfg = filter_cfg or TelegramFilterConfig(
        min_severity="info",
        category_deny_list=[],
        rate_limit_per_window=100,
        rate_limit_window_minutes=60,
        quiet_hours_start="00:00",
        quiet_hours_end="00:00",  # disable quiet hours
    )
    return TelegramNotifier(
        bot_token=token, chat_id=chat,
        session=session or MagicMock(),
        filter_config=cfg,
    )


def test_disabled_when_token_missing():
    n = TelegramNotifier(bot_token="", chat_id="42")
    assert n.enabled is False
    # send() must be a no-op that returns True
    assert n.send(title="t", body="b") is True


def test_disabled_when_chat_id_missing():
    n = TelegramNotifier(bot_token="abc", chat_id="")
    assert n.enabled is False
    assert n.send(title="t", body="b") is True


def test_send_200_marks_last_send_at(temp_db):
    session = MagicMock()
    session.post.return_value = _mock_response(200)
    n = _make_notifier(session=session)
    assert n.send(title="t", body="b") is True
    assert n._last_send_at is not None
    # Single POST to the sendMessage endpoint.
    assert session.post.call_count == 1
    url = session.post.call_args.args[0]
    assert "sendMessage" in url
    assert "/botabc/" in url


def test_send_429_enqueues_and_returns_false(temp_db):
    session = MagicMock()
    session.post.return_value = _mock_response(429, "rate-limited")
    n = _make_notifier(session=session)
    assert n.send(title="t", body="b") is False
    from backend.bot.notifications import retry_queue
    assert retry_queue.queue_depth() == 1


def test_send_4xx_drops_no_retry(temp_db):
    session = MagicMock()
    session.post.return_value = _mock_response(400, "bad request")
    n = _make_notifier(session=session)
    assert n.send(title="t", body="b") is False
    from backend.bot.notifications import retry_queue
    assert retry_queue.queue_depth() == 0


def test_send_5xx_enqueues(temp_db):
    session = MagicMock()
    session.post.return_value = _mock_response(503, "service down")
    n = _make_notifier(session=session)
    assert n.send(title="t", body="b") is False
    from backend.bot.notifications import retry_queue
    assert retry_queue.queue_depth() == 1


def test_send_network_error_enqueues(temp_db):
    import requests
    session = MagicMock()
    session.post.side_effect = requests.ConnectionError("dns")
    n = _make_notifier(session=session)
    assert n.send(title="t", body="b") is False
    from backend.bot.notifications import retry_queue
    assert retry_queue.queue_depth() == 1


def test_on_alert_is_exception_safe(temp_db):
    """A subscriber callback must never raise back to ALERT_CENTER."""
    session = MagicMock()
    session.post.side_effect = RuntimeError("explode")
    n = _make_notifier(session=session)
    # Should swallow.
    result = n.on_alert(Alert(title="t", body="b", severity="info"))
    assert result is False


def test_on_alert_respects_filter(temp_db):
    """Filtered alerts must not hit the network."""
    session = MagicMock()
    cfg = TelegramFilterConfig(
        min_severity="warning",  # blocks info
        category_deny_list=[],
        rate_limit_per_window=100, rate_limit_window_minutes=60,
        quiet_hours_start="00:00", quiet_hours_end="00:00",
    )
    n = _make_notifier(session=session, filter_cfg=cfg)
    n.on_alert(Alert(title="t", body="b", severity="info"))
    assert session.post.call_count == 0


def test_healthcheck_reports_disabled_when_no_creds():
    n = TelegramNotifier(bot_token="", chat_id="")
    h = n.healthcheck()
    assert h["status"] == "disabled"
    assert h["queue_depth"] == 0


def test_healthcheck_reports_enabled_when_configured(temp_db):
    n = _make_notifier()
    h = n.healthcheck()
    assert h["status"] in ("enabled", "degraded")
    assert h["chat_id_set"] is True
    assert h["token_set"] is True
