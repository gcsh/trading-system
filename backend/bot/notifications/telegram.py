"""Telegram bot API notifier.

Wiring contract:

  • Reads ``SETTINGS.telegram_bot_token`` and ``SETTINGS.telegram_chat_id``
    at construction. If either is empty, ``enabled`` is False and every
    operation is a no-op that returns True. The rest of the bot can
    always call ``send()`` without guarding on credentials.
  • Subscribed to ``ALERT_CENTER`` at startup via ``on_alert(alert)`` —
    that's the production fanout. ``send()`` is also exposed for ad-hoc
    callers (digest, /notifications/telegram/test).
  • Failure modes are documented inline below; the table:

      ┌────────────┬─────────────────────┐
      │ HTTP code  │ action              │
      ├────────────┼─────────────────────┤
      │ 200        │ log DEBUG, return T │
      │ 429        │ enqueue, return F   │
      │ 4xx other  │ log + drop, ret F   │
      │ 5xx        │ enqueue, return F   │
      │ net error  │ enqueue, return F   │
      └────────────┴─────────────────────┘

  • Subscribers MUST NEVER raise. ``on_alert`` swallows every exception
    and routes it to the WARNING ring buffer so the operator can see
    what happened on the in-bot Authority Spine.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Callable, Deque, Dict, Optional

import requests

from backend.bot.alerts import Alert
from backend.bot.notifications import retry_queue
from backend.bot.notifications.base import BaseNotifier
from backend.bot.notifications.filters import (
    TelegramFilterConfig,
    should_send,
)
from backend.bot.notifications.formatters import (
    TELEGRAM_MAX_CHARS,
    format_alert,
)
from backend.config import SETTINGS

logger = logging.getLogger(__name__)

# Telegram bot API base. Token is appended to the URL path.
_API_BASE = "https://api.telegram.org"

# Request timeout — short enough that a wedged Telegram doesn't stall
# the engine cycle (notifier runs on the same thread that called
# ALERT_CENTER.fire). 8s lines up with the engine's per-cycle budget.
_HTTP_TIMEOUT_SEC = 8.0


class TelegramNotifier(BaseNotifier):
    """Concrete Telegram channel — see module docstring."""

    name = "telegram"

    def __init__(
        self,
        *,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        filter_config: Optional[TelegramFilterConfig] = None,
        session: Optional[requests.Session] = None,
        config_loader: Optional[Callable[[], TelegramFilterConfig]] = None,
    ) -> None:
        self.bot_token = (bot_token if bot_token is not None
                           else SETTINGS.telegram_bot_token or "").strip()
        self.chat_id = (chat_id if chat_id is not None
                         else SETTINGS.telegram_chat_id or "").strip()
        self._static_filter = filter_config
        # config_loader lets the route layer hand the notifier a fresh
        # filter config on every send (so UI changes take effect without
        # restart). Default: pull from TUNABLES + persisted bot config.
        self._config_loader = config_loader or self._default_config_loader
        self._session = session or requests.Session()
        # Healthcheck telemetry, all guarded by a single lock.
        self._lock = threading.Lock()
        self._last_send_at: Optional[datetime] = None
        # 24h sliding-window error ring (we keep tuples of (ts, exc_repr)).
        self._error_log: Deque[tuple[datetime, str]] = deque(maxlen=500)

    # -- enable / config ---------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _default_config_loader(self) -> TelegramFilterConfig:
        """Load filter config: TUNABLES → bot_config UI override.

        Wrapped in a try/except because the DB may not be initialized
        in tests that construct the notifier in isolation.
        """
        try:
            from backend.db import session_scope
            from backend.models.config import load_config
            with session_scope() as session:
                cfg = load_config(session)
            return TelegramFilterConfig.from_dict(
                (cfg or {}).get("telegram_filters") or {},
            )
        except Exception:
            return TelegramFilterConfig.from_tunables()

    def _filter_config(self) -> TelegramFilterConfig:
        if self._static_filter is not None:
            return self._static_filter
        try:
            return self._config_loader()
        except Exception:
            return TelegramFilterConfig.from_tunables()

    # -- public API --------------------------------------------------------

    def send(
        self,
        *,
        title: str,
        body: str,
        severity: str = "info",
        category: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Build a synthetic Alert and route through the filter + HTTP path.

        Returns True on accepted delivery / queue, False on drop.
        """
        alert = Alert(
            title=title, body=body, severity=severity,
            category=category or "system",
            meta=dict(meta or {}),
        )
        text = format_alert(alert)
        return self._enqueue_or_send(text, source_alert=alert)

    def send_text(self, text: str, *, bypass_filters: bool = False) -> bool:
        """Lower-level: deliver a pre-formatted HTML string.

        Used by the digest job and the /notifications/telegram/test
        endpoint. When ``bypass_filters=True``, the message bypasses
        every filter (operator-initiated test should always reach the
        operator).
        """
        if not self.enabled:
            logger.debug(
                "telegram disabled — skipping send_text (len=%d)", len(text)
            )
            return True
        if not bypass_filters:
            # Wrap in a synthetic Alert to reuse the filter pipeline.
            synth = Alert(
                title="raw", body=text, severity="info", category="system",
            )
            cfg = self._filter_config()
            ok, reason = should_send(synth, cfg)
            if not ok:
                logger.debug("telegram send_text filtered: %s", reason)
                return False
        payload = self._build_payload(text)
        return self._dispatch(payload)

    def on_alert(self, alert: Alert) -> bool:
        """Subscriber callback registered against ALERT_CENTER.

        MUST be exception-safe. The AlertCenter subscriber loop catches
        exceptions too, but we want to log specific failure modes in
        the notifier's own healthcheck telemetry.
        """
        try:
            if not self.enabled:
                return True
            cfg = self._filter_config()
            ok, reason = should_send(alert, cfg)
            if not ok:
                logger.debug(
                    "telegram filtered alert %r: %s", alert.title, reason
                )
                return False
            text = format_alert(alert)
            return self._enqueue_or_send(text, source_alert=alert)
        except Exception as exc:
            self._record_error(repr(exc))
            logger.exception("telegram on_alert failed")
            return False

    # -- HTTP layer --------------------------------------------------------

    def _build_payload(self, text: str) -> Dict[str, Any]:
        # Hard-cap at the Telegram absolute max so we never trip a 400.
        if len(text) > TELEGRAM_MAX_CHARS:
            text = text[: TELEGRAM_MAX_CHARS - 16] + " … (truncated)"
        return {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

    def _enqueue_or_send(
        self,
        text: str,
        *,
        source_alert: Optional[Alert] = None,
    ) -> bool:
        if not self.enabled:
            return True
        payload = self._build_payload(text)
        return self._dispatch(payload)

    def _dispatch(self, payload: Dict[str, Any]) -> bool:
        """Send + classify the response. Returns True on accept/queue."""
        if not self.enabled:
            return True
        url = f"{_API_BASE}/bot{self.bot_token}/sendMessage"
        try:
            resp = self._session.post(
                url, json=payload, timeout=_HTTP_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            self._record_error(f"net: {exc!r}")
            self._enqueue(payload, last_error=f"net: {exc!r}")
            return False
        return self._classify(resp, payload)

    def _classify(
        self,
        resp: "requests.Response",
        payload: Dict[str, Any],
    ) -> bool:
        status = resp.status_code
        if 200 <= status < 300:
            with self._lock:
                self._last_send_at = datetime.utcnow()
            logger.debug("telegram sendMessage 2xx")
            return True
        if status == 429:
            self._record_error(f"429 rate-limited: {self._snip(resp.text)}")
            self._enqueue(payload, last_error="429 rate-limited")
            return False
        if 400 <= status < 500:
            # Other 4xx — Telegram will never accept this. Drop.
            self._record_error(f"{status}: {self._snip(resp.text)}")
            logger.warning(
                "telegram sendMessage %d (dropping): %s",
                status, self._snip(resp.text),
            )
            return False
        # 5xx → retry.
        self._record_error(f"{status}: {self._snip(resp.text)}")
        self._enqueue(payload, last_error=f"{status}")
        return False

    @staticmethod
    def _snip(text: Optional[str], *, maxlen: int = 200) -> str:
        if not text:
            return ""
        return text[:maxlen]

    # -- queue helpers -----------------------------------------------------

    def _enqueue(self, payload: Dict[str, Any], *, last_error: str) -> None:
        try:
            row_id = retry_queue.enqueue(payload)
            logger.info(
                "telegram message queued for retry (id=%d, reason=%s)",
                row_id, last_error,
            )
        except Exception:
            logger.exception(
                "telegram retry enqueue failed — message LOST"
            )

    def drain_queue(self, *, max_attempts: int = 5) -> Dict[str, int]:
        """Run a drain pass. Returns combined drain + sweep stats.

        Called by the scheduler job every 60s. Safe to call manually
        for testing.
        """
        if not self.enabled:
            return {"checked": 0, "delivered": 0, "rescheduled": 0,
                    "errors": 0, "swept": 0}

        def _send_one(payload: Dict[str, Any]) -> bool:
            try:
                resp = self._session.post(
                    f"{_API_BASE}/bot{self.bot_token}/sendMessage",
                    json=payload, timeout=_HTTP_TIMEOUT_SEC,
                )
            except requests.RequestException as exc:
                self._record_error(f"drain net: {exc!r}")
                return False
            # We swallow the response body — the queue's job is just
            # "did it work?". The classify path inside dispatch handles
            # detailed telemetry; we re-use _classify here but tell it
            # not to re-enqueue (it's already from the queue).
            status = resp.status_code
            if 200 <= status < 300:
                with self._lock:
                    self._last_send_at = datetime.utcnow()
                return True
            self._record_error(f"drain {status}: {self._snip(resp.text)}")
            # Non-retryable 4xx (excluding 429) → return True so the
            # queue drops it, matching the live dispatch behavior.
            if 400 <= status < 500 and status != 429:
                logger.warning(
                    "drain: dropping permanently-failed message "
                    "(status=%d): %s",
                    status, self._snip(resp.text),
                )
                return True
            return False

        stats = retry_queue.drain(_send_one)
        swept = retry_queue.sweep_failures(max_attempts=max_attempts)
        stats["swept"] = swept
        return stats

    # -- healthcheck -------------------------------------------------------

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._error_log.append((datetime.utcnow(), message[:280]))

    def healthcheck(self) -> Dict[str, Any]:
        status = "enabled" if self.enabled else "disabled"
        depth = 0
        try:
            depth = retry_queue.queue_depth()
        except Exception:
            depth = 0
        cutoff = datetime.utcnow() - timedelta(hours=24)
        with self._lock:
            errors_24h = sum(
                1 for ts, _ in self._error_log if ts >= cutoff
            )
            last = self._last_send_at.isoformat() if self._last_send_at else None
            recent_errors = [
                {"timestamp": ts.isoformat(), "error": msg}
                for ts, msg in list(self._error_log)[-5:]
            ]
        if status == "enabled" and (depth > 50 or errors_24h > 20):
            status = "degraded"
        return {
            "status": status,
            "last_send_at": last,
            "queue_depth": depth,
            "errors_24h": errors_24h,
            "recent_errors": recent_errors,
            "chat_id_set": bool(self.chat_id),
            "token_set": bool(self.bot_token),
        }


__all__ = ["TelegramNotifier"]
