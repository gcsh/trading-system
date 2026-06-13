"""Coverage for BotScheduler and trading-day helpers."""
from datetime import datetime
from unittest.mock import MagicMock

from backend.bot.engine import BotEngine
from backend.bot.scheduler import BotScheduler, is_trading_day


def test_weekend_is_not_a_trading_day():
    saturday = datetime(2026, 5, 23)  # Saturday
    sunday = datetime(2026, 5, 24)
    assert not is_trading_day(saturday)
    assert not is_trading_day(sunday)


def test_holiday_is_not_a_trading_day():
    new_years = datetime(2026, 1, 1)
    assert not is_trading_day(new_years)


def test_weekday_is_a_trading_day():
    weekday = datetime(2026, 5, 26)  # Tuesday
    assert is_trading_day(weekday)


def test_scheduler_configures_jobs():
    engine = BotEngine(executor=MagicMock())
    scheduler = BotScheduler(engine)
    scheduler.configure()
    job_count = len(scheduler.scheduler.get_jobs())
    assert job_count >= 4
    # Configure should be idempotent.
    scheduler.configure()
    assert len(scheduler.scheduler.get_jobs()) == job_count


def test_intraday_skips_when_engine_stopped():
    engine = BotEngine(executor=MagicMock())
    engine.run_cycle = MagicMock()
    scheduler = BotScheduler(engine)
    scheduler._intraday()
    engine.run_cycle.assert_not_called()


def test_intraday_runs_when_engine_running(monkeypatch):
    monkeypatch.setattr("backend.bot.scheduler.is_trading_day", lambda now=None: True)
    engine = BotEngine(executor=MagicMock())
    engine.status.running = True
    engine.run_cycle = MagicMock()
    scheduler = BotScheduler(engine)
    scheduler._intraday()
    engine.run_cycle.assert_called_once()


def test_pre_market_calls_engine(monkeypatch):
    monkeypatch.setattr("backend.bot.scheduler.is_trading_day", lambda now=None: True)
    engine = BotEngine(executor=MagicMock())
    engine.run_cycle = MagicMock()
    scheduler = BotScheduler(engine)
    scheduler._pre_market()
    engine.run_cycle.assert_called_once()


def test_post_market_resets_cycles(monkeypatch):
    monkeypatch.setattr("backend.bot.scheduler.is_trading_day", lambda now=None: True)
    engine = BotEngine(executor=MagicMock())
    engine.status.cycles = 42
    scheduler = BotScheduler(engine)
    scheduler._post_market()
    assert engine.status.cycles == 0
