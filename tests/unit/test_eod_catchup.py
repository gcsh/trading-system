"""MITS Phase 4 (P4.5) — Sunday/Monday EOD catch-up pass tests.

Pins:
  1. ``most_recent_trading_day`` walks back over weekends + holidays.
  2. ``_eod_catchup_pass`` runs when the target day has 0 EodAnalysis
     rows.
  3. ``_eod_catchup_pass`` no-ops when ≥1 row already exists.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.invariant]


def test_most_recent_trading_day_skips_weekend():
    from backend.bot.scheduler import most_recent_trading_day
    # Sunday 2026-06-07 → walks back to Friday 2026-06-05.
    result = most_recent_trading_day(today=date(2026, 6, 7))
    assert result == date(2026, 6, 5)


def test_most_recent_trading_day_skips_holiday():
    """Memorial Day 2026 falls on Monday 2026-05-25. The Tuesday after
    walking back returns Friday 2026-05-22 — skipping the holiday."""
    from backend.bot.scheduler import most_recent_trading_day
    result = most_recent_trading_day(today=date(2026, 5, 26))
    assert result == date(2026, 5, 22)


def test_most_recent_trading_day_include_today_returns_today_on_weekday():
    from backend.bot.scheduler import most_recent_trading_day
    # Friday 2026-06-05.
    result = most_recent_trading_day(
        today=date(2026, 6, 5), include_today=True,
    )
    assert result == date(2026, 6, 5)


def test_eod_catchup_runs_when_no_rows_exist():
    """Pass should call ``run_eod_pass(date=target)`` when the EodAnalysis
    table has no rows for the resolved trading day."""
    from backend.bot.scheduler import BotScheduler

    engine_mock = MagicMock()
    sched = BotScheduler(engine_mock)
    target = date(2026, 6, 5)

    with patch("backend.bot.scheduler.most_recent_trading_day",
                  return_value=target), \
            patch("backend.bot.eod_analysis.run_eod_pass") as run_mock, \
            patch("backend.db.session_scope") as scope_mock:
        # Mock session execution to return count 0.
        ctx = MagicMock()
        ctx.execute.return_value.scalar.return_value = 0
        scope_mock.return_value.__enter__.return_value = ctx
        run_mock.return_value = {"tickers_analyzed": 7}
        sched._eod_catchup_pass()
        run_mock.assert_called_once()
        assert run_mock.call_args.kwargs["date"] == target


def test_eod_catchup_noops_when_rows_exist():
    """Pass should NOT call ``run_eod_pass`` when at least one row
    already exists for the resolved trading day."""
    from backend.bot.scheduler import BotScheduler

    engine_mock = MagicMock()
    sched = BotScheduler(engine_mock)
    target = date(2026, 6, 5)

    with patch("backend.bot.scheduler.most_recent_trading_day",
                  return_value=target), \
            patch("backend.bot.eod_analysis.run_eod_pass") as run_mock, \
            patch("backend.db.session_scope") as scope_mock:
        ctx = MagicMock()
        ctx.execute.return_value.scalar.return_value = 5
        scope_mock.return_value.__enter__.return_value = ctx
        sched._eod_catchup_pass()
        run_mock.assert_not_called()


def test_eod_catchup_is_registered_on_sun_and_mon():
    """Job registration: both Sunday 10:00 ET and Monday 06:00 ET
    catch-up triggers should be present after configure()."""
    from backend.bot.scheduler import BotScheduler

    engine_mock = MagicMock()
    sched = BotScheduler(engine_mock)
    sched.configure()
    # Walk the registered jobs looking for the catch-up handler.
    catchup_jobs = [
        j for j in sched.scheduler.get_jobs()
        if getattr(j.func, "__name__", "") == "_eod_catchup_pass"
    ]
    assert len(catchup_jobs) >= 2
