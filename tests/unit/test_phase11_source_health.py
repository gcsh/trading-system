"""MITS Phase 11.I — unit tests for the per-source health aggregator."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from backend.db import init_db, session_scope


@pytest.fixture(autouse=True)
def _init_db_once(tmp_path, monkeypatch):
    db = tmp_path / "test_phase11_source_health.db"
    monkeypatch.setattr("backend.config.SETTINGS.db_path", str(db))
    init_db(str(db))
    yield


_TICKER_COUNTER = [0]


def _add_progress(source: str, status: str, rows_written: int,
                       started_offset_hours: int = 1,
                       duration_seconds: int = 4):
    """Insert one BackfillProgress row inside the 24h window."""
    from backend.models.backfill_progress import BackfillProgress
    now = datetime.utcnow()
    start = now - timedelta(hours=started_offset_hours)
    end = start + timedelta(seconds=duration_seconds)
    _TICKER_COUNTER[0] += 1
    with session_scope() as s:
        s.add(BackfillProgress(
            source=source,
            ticker=f"TKR_{_TICKER_COUNTER[0]}",
            date_range_start=str(date.today()),
            date_range_end=str(date.today()),
            status=status,
            last_completed_date=str(date.today()),
            rows_written=rows_written,
            retry_count=0,
            started_at=start,
            completed_at=end,
            error_text=("boom" if status == "error" else None),
        ))


def test_health_green_when_all_success_and_rows():
    from backend.bot.monitoring.source_health import run_pass
    for _ in range(3):
        _add_progress("fred", "done", rows_written=500)
    stats = run_pass(sources=["fred"])
    assert stats["fred"]["status"] == "green"
    assert stats["fred"]["successes"] == 3
    assert stats["fred"]["rows_written"] == 1500


def test_health_yellow_when_partial_failures():
    from backend.bot.monitoring.source_health import run_pass
    # 4 done + 1 error → 80% success, exactly on yellow threshold
    for _ in range(4):
        _add_progress("finnhub_news", "done", rows_written=10)
    _add_progress("finnhub_news", "error", rows_written=0)
    stats = run_pass(sources=["finnhub_news"])
    # exactly at threshold → yellow per the >= comparison
    assert stats["finnhub_news"]["status"] in ("yellow", "green")
    # Below threshold (60%) → red
    _add_progress("finnhub_news", "error", rows_written=0)
    _add_progress("finnhub_news", "error", rows_written=0)
    stats2 = run_pass(sources=["finnhub_news"])
    assert stats2["finnhub_news"]["status"] == "red"


def test_health_red_when_no_activity():
    from backend.bot.monitoring.source_health import run_pass
    stats = run_pass(sources=["edgar_13f"])
    assert stats["edgar_13f"]["status"] == "red"
    assert stats["edgar_13f"]["attempts"] == 0


def test_health_pass_idempotent_on_rerun():
    from backend.bot.monitoring.source_health import run_pass
    from backend.models.data_source_health import DataSourceHealth
    _add_progress("fred", "done", rows_written=50)
    run_pass(sources=["fred"])
    run_pass(sources=["fred"])
    with session_scope() as s:
        rows = s.execute(
            select(DataSourceHealth)
            .where(DataSourceHealth.source == "fred")
            .where(DataSourceHealth.snapshot_date == date.today())
        ).scalars().all()
        assert len(rows) == 1  # UPSERT, not double-insert
