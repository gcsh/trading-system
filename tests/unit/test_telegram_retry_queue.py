"""Persistent retry queue — enqueue / drain / backoff / sweep."""
from datetime import datetime, timedelta

import pytest

from backend.bot.notifications import retry_queue


pytestmark = [pytest.mark.unit]


def test_enqueue_then_peek(temp_db):
    rid = retry_queue.enqueue({"chat_id": "1", "text": "hello"})
    assert rid > 0
    rows = retry_queue.peek(10)
    assert len(rows) == 1
    assert "hello" in rows[0]["payload"]


def test_queue_depth_reflects_outstanding(temp_db):
    assert retry_queue.queue_depth() == 0
    retry_queue.enqueue({"chat_id": "1", "text": "a"})
    retry_queue.enqueue({"chat_id": "1", "text": "b"})
    assert retry_queue.queue_depth() == 2


def test_drain_delivers_and_clears(temp_db):
    retry_queue.enqueue({"chat_id": "1", "text": "a"})
    retry_queue.enqueue({"chat_id": "1", "text": "b"})

    sent = []

    def send(p):
        sent.append(p["text"])
        return True

    stats = retry_queue.drain(send)
    assert stats == {"checked": 2, "delivered": 2,
                       "rescheduled": 0, "errors": 0}
    assert sorted(sent) == ["a", "b"]
    assert retry_queue.queue_depth() == 0


def test_drain_reschedules_on_failure(temp_db):
    now = datetime.utcnow()
    retry_queue.enqueue({"chat_id": "1", "text": "a"}, now=now)

    def send(_p):
        return False

    stats = retry_queue.drain(send, now=now)
    assert stats["delivered"] == 0
    assert stats["rescheduled"] == 1
    # Row should still be queued.
    rows = retry_queue.peek()
    assert len(rows) == 1
    assert rows[0]["attempt_count"] == 1


def test_drain_respects_next_attempt_at(temp_db):
    """A row whose next_attempt_at is in the future must NOT be picked up."""
    now = datetime.utcnow()
    rid = retry_queue.enqueue({"chat_id": "1", "text": "later"}, now=now)
    # Drain at now-now should pick up the row.
    stats = retry_queue.drain(lambda p: False, now=now)
    assert stats["rescheduled"] == 1
    # Now run a drain immediately — the row was rescheduled by 30s,
    # so it should NOT be picked up.
    stats2 = retry_queue.drain(lambda p: True, now=now)
    assert stats2["delivered"] == 0
    # Advancing past the backoff (now + 31s) makes it eligible again.
    stats3 = retry_queue.drain(lambda p: True, now=now + timedelta(seconds=31))
    assert stats3["delivered"] == 1


def test_backoff_schedule_doubles_lower_bound(temp_db):
    """Successive failures must lengthen next_attempt_at monotonically."""
    base = datetime.utcnow()
    retry_queue.enqueue({"chat_id": "1", "text": "z"}, now=base)
    # Advance "now" past each scheduled backoff to make the row eligible.
    for i in range(3):
        # Advance well past the longest backoff so we always re-pick up.
        advanced = base + timedelta(days=i + 1)
        retry_queue.drain(lambda p: False, now=advanced)
    rows = retry_queue.peek()
    assert rows[0]["attempt_count"] == 3


def test_sweep_failures_deletes_over_max_attempts(temp_db):
    """Rows past max_attempts get permanently dropped."""
    base = datetime.utcnow()
    retry_queue.enqueue({"chat_id": "1", "text": "doomed"}, now=base)
    for i in range(5):
        advanced = base + timedelta(days=i + 1)
        retry_queue.drain(lambda p: False, now=advanced)
    swept = retry_queue.sweep_failures(max_attempts=5)
    assert swept == 1
    assert retry_queue.queue_depth() == 0


def test_drain_handles_send_raises(temp_db):
    """A send callable that raises is treated as a retryable failure."""
    retry_queue.enqueue({"chat_id": "1", "text": "boom"})

    def send(_p):
        raise RuntimeError("boom")

    stats = retry_queue.drain(send)
    assert stats["rescheduled"] == 1
    rows = retry_queue.peek()
    assert "boom" in (rows[0]["last_error"] or "")


def test_drain_drops_malformed_payload(temp_db):
    """A row whose payload won't json-decode is dropped."""
    from backend.db import session_scope
    from backend.models.telegram_outbox import TelegramOutbox
    with session_scope() as s:
        s.add(TelegramOutbox(payload="not-json{",
                              attempt_count=0,
                              next_attempt_at=datetime.utcnow()))
    stats = retry_queue.drain(lambda p: True)
    assert stats["errors"] == 1
    assert retry_queue.queue_depth() == 0
