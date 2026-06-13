"""MITS Phase 3 — Tomorrow's Setup Telegram digest test."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from backend.db import init_db, session_scope
from backend.models.eod_analysis import EodAnalysis


pytestmark = [pytest.mark.unit]


@pytest.fixture
def fresh_db():
    import backend.db as db_mod
    prev_engine = db_mod._engine
    prev_session = db_mod._SessionLocal
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_mod._engine = None
    db_mod._SessionLocal = None
    init_db(path)
    try:
        yield
    finally:
        db_mod._engine = prev_engine
        db_mod._SessionLocal = prev_session
        try:
            os.unlink(path)
        except OSError:
            pass


def _seed(ticker, pattern, posterior, n, score, analysis_date=None):
    analysis_date = analysis_date or date(2026, 6, 5)
    with session_scope() as s:
        s.add(EodAnalysis(
            ticker=ticker, analysis_date=analysis_date,
            patterns_fired=json.dumps([pattern]),
            top_pattern=pattern, top_posterior=posterior,
            top_sample_size=n,
            headline=f"{ticker} {pattern}",
            thesis_paragraph="paragraph",
            invalidation_json=json.dumps([]),
            rank_score=score,
        ))


def test_digest_contains_top_setups(fresh_db):
    from backend.bot.eod_analysis import format_tomorrow_digest_text
    _seed("NVDA", "bull_flag", 0.71, 400, 4.34)
    _seed("SPY", "breakout", 0.62, 250, 3.42)
    _seed("AAPL", "pullback", 0.55, 80, 2.41)
    text = format_tomorrow_digest_text(
        analysis_date=date(2026, 6, 5), limit=3,
    )
    assert text is not None
    assert "<b>Tomorrow's Setup" in text
    assert "NVDA" in text
    assert "SPY" in text
    # Limit=3 → all three rendered.
    assert "AAPL" in text


def test_digest_respects_limit(fresh_db):
    from backend.bot.eod_analysis import format_tomorrow_digest_text
    _seed("NVDA", "bull_flag", 0.71, 400, 4.34)
    _seed("SPY", "breakout", 0.62, 250, 3.42)
    _seed("AAPL", "pullback", 0.55, 80, 2.41)
    text = format_tomorrow_digest_text(
        analysis_date=date(2026, 6, 5), limit=1,
    )
    assert "NVDA" in text
    assert "SPY" not in text
    assert "AAPL" not in text


def test_digest_no_rows_returns_none(fresh_db):
    from backend.bot.eod_analysis import format_tomorrow_digest_text
    assert format_tomorrow_digest_text(
        analysis_date=date(1999, 1, 1)
    ) is None


def test_scheduler_telegram_tomorrow_dispatch(fresh_db):
    """The scheduler job calls notifier.send_text with the formatted digest."""
    _seed("NVDA", "bull_flag", 0.71, 400, 4.34)
    notifier = MagicMock()
    notifier.enabled = True

    from backend.bot.scheduler import BotScheduler

    class _StubEngine:
        class status:
            running = True

    sched = BotScheduler(engine=_StubEngine(), notifier=notifier)
    with patch("backend.bot.scheduler.is_trading_day", return_value=True), \
            patch("backend.bot.eod_analysis.format_tomorrow_digest_text",
                      return_value="<b>Tomorrow's Setup</b>\nNVDA · bull_flag · 71%"):
        sched._telegram_tomorrow_setup()
    assert notifier.send_text.called
    args, kwargs = notifier.send_text.call_args
    sent_text = args[0] if args else kwargs.get("text", "")
    assert "Tomorrow's Setup" in sent_text


def test_scheduler_telegram_tomorrow_no_op_when_disabled(fresh_db):
    """Notifier disabled → no send call."""
    notifier = MagicMock()
    notifier.enabled = False

    from backend.bot.scheduler import BotScheduler

    class _StubEngine:
        class status:
            running = True

    sched = BotScheduler(engine=_StubEngine(), notifier=notifier)
    with patch("backend.bot.scheduler.is_trading_day", return_value=True):
        sched._telegram_tomorrow_setup()
    assert not notifier.send_text.called


def test_scheduler_telegram_tomorrow_no_op_when_no_rows(fresh_db):
    """No EOD rows → no send call (graceful no-op)."""
    notifier = MagicMock()
    notifier.enabled = True

    from backend.bot.scheduler import BotScheduler

    class _StubEngine:
        class status:
            running = True

    sched = BotScheduler(engine=_StubEngine(), notifier=notifier)
    with patch("backend.bot.scheduler.is_trading_day", return_value=True):
        sched._telegram_tomorrow_setup()
    assert not notifier.send_text.called
