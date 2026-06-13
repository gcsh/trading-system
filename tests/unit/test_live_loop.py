"""Unit tests for the engine's continuous live loop."""
import asyncio
from unittest.mock import MagicMock

import pytest

from backend.bot.engine import BotEngine


@pytest.mark.asyncio
async def test_live_loop_runs_then_stops(temp_db):
    engine = BotEngine(executor=MagicMock(), market_data=MagicMock())
    engine.run_cycle = MagicMock(return_value=[])
    engine.start_live_loop(interval_sec=0.01)
    await asyncio.sleep(0.05)
    engine.stop()
    # Give the loop a moment to clean up.
    await asyncio.sleep(0.02)
    assert engine.run_cycle.call_count >= 1
    assert engine.status.running is False


@pytest.mark.asyncio
async def test_start_live_loop_is_idempotent(temp_db):
    engine = BotEngine(executor=MagicMock(), market_data=MagicMock())
    engine.run_cycle = MagicMock(return_value=[])
    engine.start_live_loop(interval_sec=0.1)
    task = engine._live_task
    engine.start_live_loop(interval_sec=0.1)
    assert engine._live_task is task
    engine.stop()


def test_stop_without_live_loop_is_safe(temp_db):
    engine = BotEngine(executor=MagicMock(), market_data=MagicMock())
    # Just shouldn't raise.
    engine.stop()
    assert engine.status.running is False
