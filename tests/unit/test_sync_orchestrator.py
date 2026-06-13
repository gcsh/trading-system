"""MITS Phase 11.G — SyncOrchestrator tests.

Locks the three contract guarantees:

  1. ``bulk_backfill`` is idempotent — re-running for a fully-completed
     window writes ZERO new rows + does not re-call the callback.
  2. ``delta_sync`` advances the watermark by exactly one day per call.
  3. Crash-resume — a chunk with a recorded ``last_completed_date``
     restarts from the day after, not the chunk start.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple

import pytest


def _make_orch():
    """Fresh SyncOrchestrator per test so registry state doesn't leak."""
    from backend.bot.data.sync_orchestrator import SyncOrchestrator
    return SyncOrchestrator()


def test_bulk_backfill_chunks_and_completes(temp_db, monkeypatch) -> None:
    from backend.bot.data.sync_orchestrator import CallbackResult, SyncOrchestrator

    orch = SyncOrchestrator()
    call_log: List[Tuple[str, date, date]] = []

    def cb(ticker: str, chunk_start: date, chunk_end: date) -> CallbackResult:
        call_log.append((ticker, chunk_start, chunk_end))
        return CallbackResult(
            last_completed_date=chunk_end,
            rows_written=(chunk_end - chunk_start).days + 1,
        )

    summary = orch.bulk_backfill(
        source="test_source", ticker="AAPL",
        start_date=date(2025, 1, 1), end_date=date(2025, 3, 31),
        callback=cb, chunk_days=30,
    )
    assert summary.total_chunks == len(call_log)
    assert summary.completed_chunks == summary.total_chunks
    assert summary.error_chunks == 0
    assert summary.rows_written > 0
    # Coverage check — every chunk in the window touched.
    first = call_log[0]
    last = call_log[-1]
    assert first[1] == date(2025, 1, 1)
    assert last[2] == date(2025, 3, 31)


def test_bulk_backfill_idempotent_on_rerun(temp_db) -> None:
    from backend.bot.data.sync_orchestrator import CallbackResult, SyncOrchestrator

    orch = SyncOrchestrator()
    call_count = {"n": 0}

    def cb(ticker, chunk_start, chunk_end):
        call_count["n"] += 1
        return CallbackResult(last_completed_date=chunk_end,
                                  rows_written=5)

    orch.bulk_backfill("idem", "MSFT",
                            date(2024, 1, 1), date(2024, 3, 31),
                            callback=cb, chunk_days=30)
    first_calls = call_count["n"]
    # Second pass — every chunk's already ``done``; callback must NOT
    # fire again.
    summary = orch.bulk_backfill("idem", "MSFT",
                                       date(2024, 1, 1), date(2024, 3, 31),
                                       callback=cb, chunk_days=30)
    assert call_count["n"] == first_calls
    assert summary.completed_chunks == 0
    assert summary.skipped_chunks == summary.total_chunks
    assert summary.rows_written == 0


def test_crash_resume_picks_up_after_last_completed_date(temp_db) -> None:
    """Simulate a crash mid-chunk by directly writing a partial
    ``last_completed_date`` into BackfillProgress, then run the same
    backfill and verify the callback receives a start AFTER the
    recorded last completed day."""
    from backend.bot.data.sync_orchestrator import CallbackResult, SyncOrchestrator
    from backend.db import session_scope
    from backend.models.backfill_progress import BackfillProgress

    orch = SyncOrchestrator()
    cb_calls: List[Tuple[date, date]] = []

    def cb(ticker, chunk_start, chunk_end):
        cb_calls.append((chunk_start, chunk_end))
        return CallbackResult(last_completed_date=chunk_end,
                                  rows_written=3)

    # Seed a partial chunk (5 days in, then "crashed").
    with session_scope() as s:
        s.add(BackfillProgress(
            source="resume_test", ticker="TSLA",
            date_range_start=date(2025, 5, 1).isoformat(),
            date_range_end=date(2025, 5, 31).isoformat(),
            status="in_progress",
            last_completed_date=date(2025, 5, 10).isoformat(),
            rows_written=10,
        ))
    summary = orch.bulk_backfill("resume_test", "TSLA",
                                       date(2025, 5, 1), date(2025, 5, 31),
                                       callback=cb, chunk_days=31)
    # Single chunk; callback should run exactly once with start = 5/11.
    assert len(cb_calls) == 1
    assert cb_calls[0][0] == date(2025, 5, 11)
    assert summary.completed_chunks == 1
    assert summary.error_chunks == 0


def test_delta_sync_advances_watermark(temp_db) -> None:
    from sqlalchemy import select

    from backend.bot.data.sync_orchestrator import CallbackResult, SyncOrchestrator
    from backend.db import session_scope
    from backend.models.data_watermark import DataWatermark

    orch = SyncOrchestrator()
    target_date = date(2026, 3, 15)
    last_seen_capture: List[date] = []

    def cb(ticker, chunk_start, chunk_end):
        last_seen_capture.append(chunk_end)
        return CallbackResult(last_completed_date=chunk_end,
                                  rows_written=2)

    orch.delta_sync("delta_src", "NVDA", callback=cb, as_of=target_date)
    with session_scope() as s:
        row = s.execute(
            select(DataWatermark)
            .where(DataWatermark.source == "delta_src")
            .where(DataWatermark.ticker == "NVDA")
        ).scalar_one()
        assert row.last_synced_through_date == target_date.isoformat()
        assert row.success == 1
        assert row.rows_last_sync > 0

    # Run delta_sync again at target+1 — should advance by exactly 1 day.
    orch.delta_sync("delta_src", "NVDA",
                          callback=cb, as_of=target_date + timedelta(days=1))
    with session_scope() as s:
        row = s.execute(
            select(DataWatermark)
            .where(DataWatermark.source == "delta_src")
            .where(DataWatermark.ticker == "NVDA")
        ).scalar_one()
        assert row.last_synced_through_date == (
            (target_date + timedelta(days=1)).isoformat()
        )


def test_retry_envelope_marks_chunk_error_after_max_attempts(
        temp_db, monkeypatch) -> None:
    from backend.bot.data import sync_orchestrator as so

    # Short-circuit retry to 2 attempts so this test stays fast.
    monkeypatch.setattr(
        "backend.config.TUNABLES.sync_max_retry_attempts", 2, raising=False)
    monkeypatch.setattr(
        "backend.config.TUNABLES.sync_retry_backoff_base_sec", 0.0,
        raising=False)

    orch = so.SyncOrchestrator()

    attempts = {"n": 0}

    def failing_cb(ticker, chunk_start, chunk_end):
        attempts["n"] += 1
        raise RuntimeError("vendor unhappy")

    summary = orch.bulk_backfill("err_src", "AMD",
                                       date(2025, 6, 1), date(2025, 6, 30),
                                       callback=failing_cb, chunk_days=30)
    assert summary.error_chunks == 1
    assert summary.completed_chunks == 0
    # Two attempts before giving up.
    assert attempts["n"] == 2

    from sqlalchemy import select

    from backend.db import session_scope
    from backend.models.backfill_progress import BackfillProgress
    with session_scope() as s:
        row = s.execute(
            select(BackfillProgress)
            .where(BackfillProgress.source == "err_src")
            .where(BackfillProgress.ticker == "AMD")
        ).scalar_one()
        assert row.status == "error"
        assert row.error_text and "vendor unhappy" in row.error_text


def test_split_into_chunks_inclusive_end() -> None:
    from backend.bot.data.sync_orchestrator import _split_into_chunks
    chunks = _split_into_chunks(date(2024, 1, 1), date(2024, 1, 10), 3)
    # Expect 4 chunks: [1-3], [4-6], [7-9], [10-10].
    assert chunks == [
        (date(2024, 1, 1), date(2024, 1, 3)),
        (date(2024, 1, 4), date(2024, 1, 6)),
        (date(2024, 1, 7), date(2024, 1, 9)),
        (date(2024, 1, 10), date(2024, 1, 10)),
    ]
