"""Notification filters: severity, category, rate-limit, quiet hours.

The four filters compose in series (cheapest first → most expensive
last). ``should_send(alert, config)`` returns ``(True, "")`` when the
alert passes all four; otherwise ``(False, reason)`` so the caller can
log or expose the reason in the healthcheck telemetry.

Noise control is non-negotiable per the operator decision lock — every
filter is enabled by default; the operator can dial each one off via
the Settings UI.

Quiet hours always honor ``critical`` severity. The intent: a
circuit-breaker trip at 3am MUST reach the operator's phone.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Deque, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from backend.bot.alerts import Alert
from backend.bot.notifications.base import severity_rank

logger = logging.getLogger(__name__)


@dataclass
class TelegramFilterConfig:
    """All four filter knobs in one place.

    Defaults match `TUNABLES`; the UI override layer
    (``/notifications/telegram/config``) hydrates this object from the
    persisted bot config.
    """

    min_severity: str = "info"

    # Categories whose alerts are blocked entirely. Empty list = allow
    # all. Operator picks from a multi-select in the Settings UI.
    category_deny_list: List[str] = field(default_factory=list)

    # Sliding-window cap per category.
    rate_limit_per_window: int = 5
    rate_limit_window_minutes: int = 10

    # Quiet hours in HH:MM 24h format. When current local time is
    # within [start, end) AND severity < critical, the message is
    # dropped. Wraparound (start > end, e.g. 22:00→07:00) is supported.
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"
    quiet_hours_tz: str = "America/Los_Angeles"

    @classmethod
    def from_tunables(cls) -> "TelegramFilterConfig":
        """Hydrate from the global TUNABLES — picks up env-var overrides."""
        from backend.config import TUNABLES
        return cls(
            min_severity=str(TUNABLES.telegram_min_severity or "info"),
            category_deny_list=[],
            rate_limit_per_window=int(
                TUNABLES.telegram_rate_limit_per_category_per_window
            ),
            rate_limit_window_minutes=int(
                TUNABLES.telegram_rate_limit_window_minutes
            ),
            quiet_hours_start=str(TUNABLES.telegram_quiet_hours_start),
            quiet_hours_end=str(TUNABLES.telegram_quiet_hours_end),
            quiet_hours_tz=str(TUNABLES.telegram_quiet_hours_tz),
        )

    @classmethod
    def from_dict(cls, data: Optional[Dict]) -> "TelegramFilterConfig":
        """Merge a UI-saved dict onto the env defaults."""
        base = cls.from_tunables()
        if not data:
            return base
        if "min_severity" in data and data["min_severity"]:
            base.min_severity = str(data["min_severity"])
        if "category_deny_list" in data and isinstance(
            data["category_deny_list"], list
        ):
            base.category_deny_list = [str(c) for c in data["category_deny_list"]]
        if "rate_limit_per_window" in data:
            try:
                base.rate_limit_per_window = max(1, int(
                    data["rate_limit_per_window"]
                ))
            except (TypeError, ValueError):
                pass
        if "rate_limit_window_minutes" in data:
            try:
                base.rate_limit_window_minutes = max(1, int(
                    data["rate_limit_window_minutes"]
                ))
            except (TypeError, ValueError):
                pass
        if "quiet_hours_start" in data and data["quiet_hours_start"]:
            base.quiet_hours_start = str(data["quiet_hours_start"])
        if "quiet_hours_end" in data and data["quiet_hours_end"]:
            base.quiet_hours_end = str(data["quiet_hours_end"])
        if "quiet_hours_tz" in data and data["quiet_hours_tz"]:
            base.quiet_hours_tz = str(data["quiet_hours_tz"])
        return base

    def to_dict(self) -> Dict:
        return {
            "min_severity": self.min_severity,
            "category_deny_list": list(self.category_deny_list),
            "rate_limit_per_window": self.rate_limit_per_window,
            "rate_limit_window_minutes": self.rate_limit_window_minutes,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
            "quiet_hours_tz": self.quiet_hours_tz,
        }


def _parse_hhmm(value: str, default: time) -> time:
    try:
        hh, mm = value.split(":")[:2]
        return time(int(hh), int(mm))
    except Exception:
        return default


def _in_quiet_window(
    now: datetime,
    start: str,
    end: str,
    tz_name: str,
) -> bool:
    """True when `now` (UTC) falls inside [start, end) in the configured tz.

    Wraparound supported (start > end). When start == end, quiet hours
    are effectively disabled (window has zero length).
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    if now.tzinfo is None:
        # Treat naive datetimes as UTC (matches the rest of the codebase).
        now_local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    else:
        now_local = now.astimezone(tz)
    cur = now_local.time()
    s = _parse_hhmm(start, time(22, 0))
    e = _parse_hhmm(end, time(7, 0))
    if s == e:
        return False
    if s < e:
        return s <= cur < e
    # Wraparound: e.g. 22:00 → 07:00.
    return cur >= s or cur < e


class _RateLimiter:
    """In-process sliding-window rate limiter, keyed by category.

    Not durable across restarts — that's fine: a restart resets the
    window and the operator gets a fresh budget. The retry queue is
    where durability belongs; rate-limit counters are ephemeral.
    """

    def __init__(self) -> None:
        self._buckets: Dict[str, Deque[datetime]] = {}
        self._lock = threading.Lock()

    def admit(
        self,
        category: str,
        *,
        max_per_window: int,
        window_minutes: int,
        now: Optional[datetime] = None,
    ) -> bool:
        """Return True if the alert is admitted, False if rate-limited."""
        now = now or datetime.utcnow()
        cutoff = now.timestamp() - (window_minutes * 60)
        with self._lock:
            bucket = self._buckets.setdefault(category, deque())
            # Drop expired timestamps.
            while bucket and bucket[0].timestamp() < cutoff:
                bucket.popleft()
            if len(bucket) >= max_per_window:
                return False
            bucket.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


# Module-level singleton so the filter is shared across the notifier
# subscriber + the digest job + the test-send route. Tests use reset().
_LIMITER = _RateLimiter()


def reset_rate_limiter() -> None:
    """Test hook: clear the in-process rate-limit counters."""
    _LIMITER.reset()


def should_send(
    alert: Alert,
    config: TelegramFilterConfig,
    *,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Run the four filters in order. Returns (admitted, reason)."""
    now = now or datetime.utcnow()
    sev = (alert.severity or "info").lower()
    category = (alert.category or "uncategorized").lower()

    # 1. Severity floor.
    if severity_rank(sev) < severity_rank(config.min_severity):
        return False, f"severity '{sev}' < min '{config.min_severity}'"

    # 2. Category deny-list (case-insensitive comparison).
    deny = {c.lower() for c in (config.category_deny_list or [])}
    if category in deny:
        return False, f"category '{category}' is denied"

    # 3. Quiet hours — critical always passes.
    if sev != "critical" and _in_quiet_window(
        now,
        config.quiet_hours_start,
        config.quiet_hours_end,
        config.quiet_hours_tz,
    ):
        return False, (
            f"quiet hours {config.quiet_hours_start}"
            f"→{config.quiet_hours_end} {config.quiet_hours_tz}"
        )

    # 4. Per-category rate limit. Critical also bypasses to avoid a
    # cascade of warnings burning a critical's slot.
    if sev != "critical":
        if not _LIMITER.admit(
            category,
            max_per_window=config.rate_limit_per_window,
            window_minutes=config.rate_limit_window_minutes,
            now=now,
        ):
            return False, (
                f"rate-limited: >{config.rate_limit_per_window} "
                f"in {config.rate_limit_window_minutes}m for '{category}'"
            )

    return True, ""


__all__ = [
    "TelegramFilterConfig",
    "should_send",
    "reset_rate_limiter",
]
