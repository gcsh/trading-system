"""Alert hub: classifies events and pushes them onto the WebSocket stream.

Severity ladder: ``info`` < ``success`` < ``warning`` < ``danger``. The UI
maps each to a browser-notification priority and a visual treatment in the
AlertsCenter dropdown.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Deque, List, Optional

logger = logging.getLogger(__name__)

MAX_HISTORY = 200

# Type alias for a subscriber: a callable receiving the full Alert.
# Subscribers run synchronously on the fire() thread. Exceptions are
# caught + logged so one bad subscriber can never block the others or
# the broadcaster.
AlertSubscriber = Callable[["Alert"], Any]


@dataclass
class Alert:
    title: str
    body: str
    severity: str = "info"  # info | success | warning | danger | critical
    category: str = "signal"  # signal | order | risk | system | ai
    ticker: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": "alert",
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "category": self.category,
            "ticker": self.ticker,
            "timestamp": self.timestamp,
            "meta": self.meta,
        }


class AlertCenter:
    """Holds a bounded history of recent alerts and broadcasts new ones.

    Subscribers (added via :meth:`subscribe`) fire on every alert
    BEFORE the legacy WebSocket broadcaster — that lets the Telegram
    notifier observe everything without blocking the UI stream. One
    bad subscriber must NEVER affect the others; all callback
    exceptions are caught + logged.
    """

    def __init__(self, broadcaster: Optional[Callable[[dict], Any]] = None) -> None:
        self.history: Deque[Alert] = deque(maxlen=MAX_HISTORY)
        self.broadcaster = broadcaster
        self._subscribers: List[AlertSubscriber] = []
        self._sub_lock = threading.Lock()

    def attach(self, broadcaster: Callable[[dict], Any]) -> None:
        self.broadcaster = broadcaster

    def subscribe(self, callback: AlertSubscriber) -> AlertSubscriber:
        """Register `callback(Alert)` to be invoked on every fire().

        Idempotent: registering the same callable twice is a no-op
        (handy when a startup hook re-runs in dev reload). Returns
        the callback so it can be used as a decorator if desired.
        """
        with self._sub_lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)
        return callback

    def unsubscribe(self, callback: AlertSubscriber) -> bool:
        with self._sub_lock:
            try:
                self._subscribers.remove(callback)
                return True
            except ValueError:
                return False

    def _dispatch_subscribers(self, alert: Alert) -> None:
        """Fan the alert out to every subscriber. Isolated try/except
        per subscriber so one explosion never silences the rest."""
        with self._sub_lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(alert)
            except Exception:
                logger.exception(
                    "alert subscriber %r raised — continuing", cb,
                )

    def fire(self, alert: Alert) -> Alert:
        self.history.append(alert)
        # Subscribers first so a slow WebSocket broadcaster never
        # delays the Telegram notifier (which itself enqueues async).
        self._dispatch_subscribers(alert)
        if self.broadcaster:
            try:
                result = self.broadcaster(alert.to_dict())
                if asyncio.iscoroutine(result):
                    try:
                        loop = asyncio.get_event_loop()
                    except RuntimeError:
                        loop = None
                    if loop and loop.is_running():
                        asyncio.create_task(result)
                    else:
                        asyncio.run(result)
            except Exception:
                logger.exception("alert broadcast failed")
        return alert

    def fire_from_event(self, event: dict) -> Optional[Alert]:
        """Convert a trade-engine event into an alert (if it deserves one)."""
        status = event.get("status")
        ticker = event.get("ticker")
        action = event.get("action")
        if not status:
            return None
        if status == "submitted":
            return self.fire(
                Alert(
                    title=f"{action} {ticker}",
                    body=event.get("reason") or "order submitted",
                    severity="success",
                    category="order",
                    ticker=ticker,
                    meta={"order_id": event.get("order_id")},
                )
            )
        if status == "signal_only":
            return self.fire(
                Alert(
                    title=f"Signal: {action} {ticker}",
                    body=event.get("reason") or "auto-exec off",
                    severity="info",
                    category="signal",
                    ticker=ticker,
                )
            )
        if status == "rejected":
            return self.fire(
                Alert(
                    title=f"Rejected {action} {ticker}",
                    body=event.get("risk") or event.get("reason") or "risk rejected",
                    severity="warning",
                    category="risk",
                    ticker=ticker,
                )
            )
        if status == "failed":
            return self.fire(
                Alert(
                    title=f"Order failed: {ticker}",
                    body=event.get("reason") or "broker error",
                    severity="danger",
                    category="order",
                    ticker=ticker,
                )
            )
        return None

    def recent(self, limit: int = 50) -> List[dict]:
        return [a.to_dict() for a in list(self.history)[-limit:][::-1]]


ALERT_CENTER = AlertCenter()
