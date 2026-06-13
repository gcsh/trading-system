"""Telegram filters — severity / category / rate-limit / quiet hours."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.bot.alerts import Alert
from backend.bot.notifications.filters import (
    TelegramFilterConfig,
    reset_rate_limiter,
    should_send,
)


pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _isolation():
    reset_rate_limiter()
    yield
    reset_rate_limiter()


def _cfg(**overrides) -> TelegramFilterConfig:
    base = dict(
        min_severity="info",
        category_deny_list=[],
        rate_limit_per_window=100,
        rate_limit_window_minutes=60,
        quiet_hours_start="00:00",
        quiet_hours_end="00:00",  # disabled
        quiet_hours_tz="UTC",
    )
    base.update(overrides)
    return TelegramFilterConfig(**base)


# -- severity --------------------------------------------------------------

def test_severity_passes_when_at_or_above_threshold():
    cfg = _cfg(min_severity="warning")
    ok, _ = should_send(Alert(title="t", body="b", severity="warning"), cfg)
    assert ok is True


def test_severity_rejects_below_threshold():
    cfg = _cfg(min_severity="warning")
    ok, reason = should_send(
        Alert(title="t", body="b", severity="info"), cfg,
    )
    assert ok is False
    assert "severity" in reason


def test_critical_always_passes_min_severity():
    cfg = _cfg(min_severity="critical")
    ok, _ = should_send(
        Alert(title="t", body="b", severity="critical"), cfg,
    )
    assert ok is True


# -- category --------------------------------------------------------------

def test_category_deny_list_blocks():
    cfg = _cfg(category_deny_list=["risk"])
    ok, reason = should_send(
        Alert(title="t", body="b", severity="info", category="risk"), cfg,
    )
    assert ok is False
    assert "category" in reason


def test_category_deny_list_is_case_insensitive():
    cfg = _cfg(category_deny_list=["RISK"])
    ok, _ = should_send(
        Alert(title="t", body="b", severity="info", category="risk"), cfg,
    )
    assert ok is False


# -- rate limit ------------------------------------------------------------

def test_rate_limit_admits_under_threshold():
    cfg = _cfg(rate_limit_per_window=3, rate_limit_window_minutes=10)
    for _ in range(3):
        ok, _ = should_send(
            Alert(title="t", body="b", severity="info", category="signal"),
            cfg,
        )
        assert ok is True


def test_rate_limit_rejects_at_threshold():
    cfg = _cfg(rate_limit_per_window=2, rate_limit_window_minutes=10)
    for _ in range(2):
        should_send(Alert(title="t", body="b", category="signal"), cfg)
    ok, reason = should_send(
        Alert(title="t", body="b", category="signal"), cfg,
    )
    assert ok is False
    assert "rate-limited" in reason


def test_rate_limit_per_category_isolated():
    """Hitting the limit on one category does not block others."""
    cfg = _cfg(rate_limit_per_window=1, rate_limit_window_minutes=10)
    ok1, _ = should_send(
        Alert(title="t", body="b", category="risk"), cfg,
    )
    assert ok1 is True
    ok2, _ = should_send(
        Alert(title="t", body="b", category="risk"), cfg,
    )
    assert ok2 is False
    # Different category still admitted.
    ok3, _ = should_send(
        Alert(title="t", body="b", category="signal"), cfg,
    )
    assert ok3 is True


def test_rate_limit_critical_bypasses():
    cfg = _cfg(rate_limit_per_window=1, rate_limit_window_minutes=10)
    should_send(Alert(title="t", body="b", category="risk"), cfg)
    ok, _ = should_send(
        Alert(title="t", body="b", severity="critical", category="risk"),
        cfg,
    )
    assert ok is True


# -- quiet hours -----------------------------------------------------------

def test_quiet_hours_block_non_critical():
    cfg = _cfg(
        quiet_hours_start="00:00",
        quiet_hours_end="23:59",   # block all day in UTC
        quiet_hours_tz="UTC",
    )
    # Pick a time inside the window.
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    ok, reason = should_send(
        Alert(title="t", body="b", severity="warning"),
        cfg, now=now,
    )
    assert ok is False
    assert "quiet" in reason


def test_quiet_hours_admit_critical():
    cfg = _cfg(
        quiet_hours_start="00:00", quiet_hours_end="23:59",
        quiet_hours_tz="UTC",
    )
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    ok, _ = should_send(
        Alert(title="t", body="b", severity="critical"),
        cfg, now=now,
    )
    assert ok is True


def test_quiet_hours_wraparound():
    """Quiet window 22:00 → 07:00 wraps across midnight in UTC."""
    cfg = _cfg(
        quiet_hours_start="22:00", quiet_hours_end="07:00",
        quiet_hours_tz="UTC",
    )
    # 3am UTC → inside the window.
    now = datetime(2026, 6, 5, 3, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    ok, _ = should_send(
        Alert(title="t", body="b", severity="warning"),
        cfg, now=now,
    )
    assert ok is False
    # 10am UTC → outside.
    now2 = datetime(2026, 6, 5, 10, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    ok2, _ = should_send(
        Alert(title="t", body="b", severity="warning"),
        cfg, now=now2,
    )
    assert ok2 is True


def test_quiet_hours_disabled_when_start_equals_end():
    cfg = _cfg(
        quiet_hours_start="08:00", quiet_hours_end="08:00",
        quiet_hours_tz="UTC",
    )
    # Even at 8am UTC, zero-length window means everything passes.
    now = datetime(2026, 6, 5, 8, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    ok, _ = should_send(
        Alert(title="t", body="b", severity="info"), cfg, now=now,
    )
    assert ok is True


def test_quiet_hours_respects_tz():
    """22:00 PT (= 06:00 UTC next day) at 06:30 UTC should be quiet."""
    cfg = _cfg(
        quiet_hours_start="22:00",
        quiet_hours_end="07:00",
        quiet_hours_tz="America/Los_Angeles",
    )
    # 06:30 UTC → 23:30 PT on the previous day → inside PT quiet window
    now = datetime(2026, 6, 5, 6, 30, tzinfo=timezone.utc).replace(tzinfo=None)
    ok, _ = should_send(
        Alert(title="t", body="b", severity="warning"), cfg, now=now,
    )
    assert ok is False
