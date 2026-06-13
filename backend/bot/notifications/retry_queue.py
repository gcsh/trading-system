"""SQLite-backed retry queue for the Telegram notifier.

A queued row is one Telegram ``sendMessage`` payload that the channel
deferred (rate-limited, 5xx, or net error). The drain job re-attempts
each eligible row; on success the row is deleted, on failure the row
is rescheduled with exponential backoff.

The backoff schedule is fixed — a single tunable would let the operator
turn a 5-minute outage into a permanent message loss, so this stays
in code. Operator-tunable knobs (max attempts, drain frequency) live
in `Tunables` instead.

All public functions are idempotent: ``enqueue`` accepts duplicate
payloads (Telegram dedup is the operator's problem at the message
level, not ours); ``drain`` re-runs are safe.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select

from backend.db import session_scope
from backend.models.telegram_outbox import TelegramOutbox

logger = logging.getLogger(__name__)


# Backoff schedule. Index = attempt number (0-based). Attempt 0 (the
# initial send) wasn't queued — its failure is what created the row at
# attempt 1, so the table here maps "after this many attempts, wait N".
# Schedule: 30s, 2min, 10min, 1h, 6h.
_BACKOFF_SECONDS = (30, 120, 600, 3600, 21600)


def _delay_for(attempt_count: int) -> timedelta:
    """Pick the backoff for the next attempt given how many we've made.

    Index is clamped to the last entry so attempts beyond the schedule
    just keep using the longest wait. Sweeper deletes those rows
    eventually, so the clamp is just a safety net.
    """
    idx = max(0, min(attempt_count, len(_BACKOFF_SECONDS) - 1))
    return timedelta(seconds=_BACKOFF_SECONDS[idx])


def enqueue(payload: Dict[str, Any], *, now: Optional[datetime] = None) -> int:
    """Persist a payload for later delivery. Returns the new row id.

    The next-attempt time defaults to ``now`` because the caller has
    already burned one attempt — we want the drain job to re-pick it
    up on the next sweep.
    """
    now = now or datetime.utcnow()
    serialized = json.dumps(payload, default=str)
    with session_scope() as s:
        row = TelegramOutbox(
            payload=serialized,
            attempt_count=0,
            next_attempt_at=now,
            created_at=now,
        )
        s.add(row)
        s.flush()
        return int(row.id)


def queue_depth() -> int:
    """Number of rows currently queued (any state)."""
    with session_scope() as s:
        return int(s.query(TelegramOutbox).count())


def drain(
    send_callable: Callable[[Dict[str, Any]], bool],
    *,
    now: Optional[datetime] = None,
    limit: int = 100,
) -> Dict[str, int]:
    """Walk every eligible row and re-attempt delivery.

    ``send_callable(payload)`` MUST return True on durable success
    (delete row), False on retryable failure (reschedule), or raise
    on unexpected error (treated as a retryable failure).

    Returns a stats dict: ``{checked, delivered, rescheduled, errors}``.
    """
    now = now or datetime.utcnow()
    stats = {"checked": 0, "delivered": 0, "rescheduled": 0, "errors": 0}
    with session_scope() as s:
        eligible: List[TelegramOutbox] = list(
            s.execute(
                select(TelegramOutbox)
                .where(TelegramOutbox.next_attempt_at <= now)
                .order_by(TelegramOutbox.next_attempt_at.asc())
                .limit(limit)
            ).scalars()
        )
        for row in eligible:
            stats["checked"] += 1
            try:
                payload = json.loads(row.payload)
            except Exception:
                # Corrupted payload — drop it; we have nothing we can
                # do with malformed JSON.
                logger.warning("dropping malformed outbox row id=%s", row.id)
                s.delete(row)
                stats["errors"] += 1
                continue
            try:
                ok = send_callable(payload)
            except Exception as exc:  # treat as transient
                ok = False
                row.last_error = repr(exc)[:280]
            if ok:
                s.delete(row)
                stats["delivered"] += 1
            else:
                # Pick the backoff for THIS just-failed attempt
                # (attempt_count before increment), then bump the
                # counter. Result: attempt 0 → 30s, attempt 1 → 2min, ...
                row.next_attempt_at = now + _delay_for(
                    row.attempt_count or 0
                )
                row.attempt_count = (row.attempt_count or 0) + 1
                stats["rescheduled"] += 1
    return stats


def sweep_failures(*, max_attempts: int = 5,
                     now: Optional[datetime] = None) -> int:
    """Delete rows that have exhausted their retry budget.

    Called by the scheduler after each drain — keeps the queue from
    growing unbounded during a multi-day Telegram outage. Returns the
    number of rows deleted.
    """
    deleted = 0
    with session_scope() as s:
        rows = s.query(TelegramOutbox).filter(
            TelegramOutbox.attempt_count >= max_attempts
        ).all()
        for row in rows:
            s.delete(row)
            deleted += 1
    if deleted:
        logger.warning(
            "telegram outbox swept %d permanently-failed messages "
            "(max_attempts=%d)",
            deleted, max_attempts,
        )
    return deleted


def peek(limit: int = 10) -> List[Dict[str, Any]]:
    """Return up to ``limit`` queued rows as dicts (newest last).

    Used by the healthcheck route + tests; never mutates state.
    """
    with session_scope() as s:
        rows = list(
            s.execute(
                select(TelegramOutbox)
                .order_by(TelegramOutbox.id.asc())
                .limit(limit)
            ).scalars()
        )
        return [r.to_dict() for r in rows]


__all__ = [
    "enqueue",
    "drain",
    "sweep_failures",
    "queue_depth",
    "peek",
]
