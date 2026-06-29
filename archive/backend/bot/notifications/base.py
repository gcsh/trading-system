"""Notifier interface — every external notification channel (Telegram now,
SMS / Slack / etc. in the future) implements this surface so the rest of
the system never depends on the channel.

Severity ladder
---------------

   info  <  success  <  warning  <  danger  <  critical

`critical` is the only severity that pierces quiet hours by default.
Existing AlertCenter usage continues to use "info | success | warning |
danger" — the filter layer treats unknown / missing severities as info.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# Canonical severity order (low → high). Kept here so every consumer
# (filters, formatters, UI dropdowns) draws from one place.
SEVERITY_ORDER = ("info", "success", "warning", "danger", "critical")


def severity_rank(sev: str | None) -> int:
    """Index of `sev` in SEVERITY_ORDER, defaulting to 0 (info) when unknown.

    Used by the min-severity filter — `rank(alert.severity) >= rank(min_sev)`.
    """
    try:
        return SEVERITY_ORDER.index((sev or "info").lower())
    except ValueError:
        return 0


class BaseNotifier(ABC):
    """Abstract notification channel.

    Implementations must be safe to call from any thread; the alert
    subscriber pipeline can fire from the scheduler, from the engine
    loop, or from the request thread.
    """

    name: str = "base"

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """True when the channel is fully configured and ready to send.

        A disabled notifier MUST behave as a no-op: send() returns True
        without touching the network, healthcheck() reports
        ``status='disabled'``. This lets the rest of the system always
        call ``notifier.send(...)`` without worrying about credentials.
        """

    @abstractmethod
    def send(
        self,
        *,
        title: str,
        body: str,
        severity: str = "info",
        category: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Send a single message.

        Returns True when the message was either delivered, queued for
        retry, or correctly suppressed (e.g. notifier disabled, message
        filtered). Returns False only when the message was permanently
        rejected with no retry attempt — caller can decide whether to
        log + drop or escalate.
        """

    @abstractmethod
    def healthcheck(self) -> Dict[str, Any]:
        """Lightweight introspection. Should never raise — all errors
        must be folded into the returned dict.

        Expected keys:
            status         — "enabled" | "disabled" | "degraded"
            last_send_at   — ISO timestamp of last successful send (or None)
            queue_depth    — int, current retry queue depth
            errors_24h     — int, count of send errors in the last 24h
        """


__all__ = ["BaseNotifier", "SEVERITY_ORDER", "severity_rank"]
