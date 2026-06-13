"""Formatter unit tests — HTML safety + 4096-char truncation."""
import pytest

from backend.bot.alerts import Alert
from backend.bot.notifications.formatters import (
    TARGET_CHARS,
    TELEGRAM_MAX_CHARS,
    format_alert,
    format_pnl,
    format_system_warning,
    format_test_message,
    format_trade,
    safe_json_meta,
)


pytestmark = [pytest.mark.unit]


def test_format_alert_renders_html_layout():
    alert = Alert(
        title="Order filled",
        body="AAPL BUY_STOCK 5 shares @ $200.00",
        severity="success",
        category="order",
        ticker="AAPL",
    )
    out = format_alert(alert)
    assert "<b>Order filled</b>" in out
    assert "AAPL BUY_STOCK" in out
    assert "[+]" in out  # success badge


def test_format_alert_escapes_user_input():
    """No raw < > & in the output, regardless of input."""
    alert = Alert(
        title="<script>alert('x')</script>",
        body="<b>bold</b> & <em>em</em>",
        severity="warning",
    )
    out = format_alert(alert)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out or "&amp;lt;script&amp;gt;" in out
    assert "&amp;" in out or "&lt;b&gt;" in out


def test_format_alert_truncates_long_body():
    long_body = "x" * (TARGET_CHARS + 500)
    alert = Alert(title="t", body=long_body)
    out = format_alert(alert)
    assert len(out) <= TARGET_CHARS
    assert "truncated" in out


def test_format_alert_stays_under_telegram_max():
    """Even with no truncation request, output must never exceed
    the absolute Telegram limit."""
    huge = "y" * (TELEGRAM_MAX_CHARS * 2)
    alert = Alert(title=huge, body=huge)
    out = format_alert(alert)
    assert len(out) <= TELEGRAM_MAX_CHARS


def test_format_alert_unknown_severity_defaults_to_info_badge():
    alert = Alert(title="t", body="b", severity="weird")
    out = format_alert(alert)
    assert "[i]" in out  # info fallback


def test_format_trade_compact():
    event = {
        "ticker": "TSLA", "action": "BUY_STOCK", "status": "submitted",
        "reason": "momentum", "quantity": 10, "price": 200,
    }
    out = format_trade(event)
    assert "TSLA" in out
    assert "BUY_STOCK" in out
    assert "qty 10" in out


def test_format_pnl_signed_money():
    out = format_pnl({"daily_pnl": -123.45, "realized_pnl": 0,
                        "unrealized_pnl": 50.25})
    assert "-$123.45" in out
    assert "$50.25" in out


def test_format_system_warning_carries_logger_and_path():
    out = format_system_warning({
        "level": "ERROR", "message": "boom",
        "logger": "backend.foo", "path": "foo.py", "line": 42,
    })
    assert "SYSTEM ERROR" in out
    assert "backend.foo" in out
    assert "foo.py" in out
    assert "42" in out


def test_format_test_message_self_contained():
    out = format_test_message()
    assert "<b>" in out
    assert "test message" in out


def test_safe_json_meta_truncates():
    huge = {"x": "y" * 10000}
    out = safe_json_meta(huge)
    assert len(out) < 2000
    assert "<pre>" in out


def test_safe_json_meta_empty_returns_empty():
    assert safe_json_meta(None) == ""
    assert safe_json_meta({}) == ""
