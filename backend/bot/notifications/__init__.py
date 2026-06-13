"""Telegram notification pipeline (and pluggable future channels).

Wiring:

  ALERT_CENTER.fire(alert)
        │
        ▼
  notifier.on_alert(alert)        # subscribed at app startup
        │  filters (severity / category / rate-limit / quiet hours)
        ▼
  notifier.send(...)
        │  HTTP POST sendMessage
        ├──▶ 200  → logged at DEBUG
        ├──▶ 429  → retry queue (exponential backoff)
        ├──▶ 4xx  → log + drop
        └──▶ 5xx / net error → retry queue

The package layout:

  base.py          BaseNotifier abstract interface
  telegram.py      TelegramNotifier (Telegram bot API)
  filters.py       severity / category / rate-limit / quiet-hours
  formatters.py    HTML-safe Alert / trade / digest formatters
  retry_queue.py   SQLite-backed durable outbox
  digest.py        end-of-day summary builder
  commands.py      bidirectional /status, /pause, ... handlers
"""
from __future__ import annotations

from backend.bot.notifications.base import BaseNotifier

__all__ = ["BaseNotifier"]
