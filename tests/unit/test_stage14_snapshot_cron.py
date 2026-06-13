"""Stage-14 regime snapshot scheduler job.

Pinned:
  • _regime_snapshot is a no-op when no MarketState exists
  • _regime_snapshot is a no-op on weekends / holidays
  • _regime_snapshot writes a row when state + trading day both hold
  • Job is registered in scheduler.configure() with the right cron
"""
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from backend.bot.scheduler import BotScheduler
from backend.bot.state import build_market_state, reset_latest, set_latest


@pytest.fixture(autouse=True)
def _isolate():
    reset_latest()
    yield
    reset_latest()


def _scheduler():
    engine = MagicMock()
    return BotScheduler(engine)


class TestRegimeSnapshotJob:
    def test_noop_when_no_state(self, temp_db):
        sch = _scheduler()
        # No latest state set — should silently skip without crashing.
        sch._regime_snapshot()
        # Verify no row got written
        from backend.db import session_scope
        from backend.models.regime_episode import RegimeEpisodeSnapshot
        with session_scope() as s:
            count = s.query(RegimeEpisodeSnapshot).count()
            assert count == 0

    def test_noop_on_non_trading_day(self, temp_db):
        set_latest(build_market_state(regime={"trend": "bullish"}))
        sch = _scheduler()
        # Mock is_trading_day to False — weekend / holiday case
        with patch("backend.bot.scheduler.is_trading_day", return_value=False):
            sch._regime_snapshot()
        from backend.db import session_scope
        from backend.models.regime_episode import RegimeEpisodeSnapshot
        with session_scope() as s:
            assert s.query(RegimeEpisodeSnapshot).count() == 0

    def test_writes_row_on_trading_day_with_state(self, temp_db):
        set_latest(build_market_state(
            regime={"trend": "bullish", "volatility": "normal",
                       "gamma": "long_gamma"},
            features={"vix": 16, "iv_rank": 35},
        ))
        sch = _scheduler()
        with patch("backend.bot.scheduler.is_trading_day", return_value=True):
            sch._regime_snapshot()
        from backend.db import session_scope
        from backend.models.regime_episode import RegimeEpisodeSnapshot
        with session_scope() as s:
            rows = s.query(RegimeEpisodeSnapshot).all()
            assert len(rows) == 1
            assert rows[0].trend == "bullish"
            assert rows[0].vix == 16


class TestJobRegistration:
    def test_configure_registers_snapshot_job(self):
        sch = _scheduler()
        sch.configure()
        job_funcs = [str(j.func) for j in sch.scheduler.get_jobs()]
        assert any("_regime_snapshot" in f for f in job_funcs), \
            f"_regime_snapshot job not found in: {job_funcs}"
