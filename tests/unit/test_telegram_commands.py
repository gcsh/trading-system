"""Each bidirectional command returns the expected reply shape."""
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from backend.bot.notifications.commands import COMMANDS, dispatch, parse_command


pytestmark = [pytest.mark.unit]


def _engine_stub(*, running=True, cycles=42, positions=None,
                    last_cycle="2026-06-05T18:00:00", daily_pnl=125.0):
    engine = MagicMock()
    engine.status.running = running
    engine.status.cycles = cycles
    engine.status.last_cycle_at = last_cycle
    engine.status.active_strategy = "adaptive"
    engine.status.market_regime = "trending_up"
    engine.status.daily_pnl = daily_pnl
    engine.executor.positions.return_value = positions or []
    return engine


def test_parse_command_recognizes_root_commands():
    assert parse_command("/status") == ("/status", [])
    assert parse_command("/last 10") == ("/last", ["10"])
    assert parse_command("/STATUS") == ("/status", [])


def test_parse_command_strips_bot_mention():
    """Telegram appends @bot_name in groups; we must accept that."""
    assert parse_command("/status@trading_bot") == ("/status", [])


def test_parse_command_unknown_returns_none():
    assert parse_command("/jibberish") == (None, [])
    assert parse_command("hi") == (None, [])
    assert parse_command("") == (None, [])


def test_status_command(temp_db):
    engine = _engine_stub()
    out = dispatch("/status", engine)
    assert "<b>Bot status</b>" in out
    assert "running" in out
    assert "42" in out  # cycles
    assert "trending_up" in out


def test_pause_command_calls_engine_stop(temp_db):
    engine = _engine_stub()
    out = dispatch("/pause", engine)
    engine.stop.assert_called_once()
    assert "paused" in out.lower()


def test_resume_command_calls_start_live_loop(temp_db):
    engine = _engine_stub()
    out = dispatch("/resume", engine)
    engine.start_live_loop.assert_called_once()
    assert "resumed" in out.lower()


def test_positions_command_empty(temp_db):
    engine = _engine_stub(positions=[])
    out = dispatch("/positions", engine)
    assert "none" in out.lower()


def test_positions_command_lists_holdings(temp_db):
    engine = _engine_stub(positions=[
        {"ticker": "AAPL", "quantity": 10, "average_price": 200.5,
         "instrument": "stock", "unrealized_pnl": 25.0},
        {"ticker": "TSLA", "quantity": 5, "average_price": 300.0,
         "instrument": "stock"},
    ])
    out = dispatch("/positions", engine)
    assert "AAPL" in out
    assert "TSLA" in out
    assert "(2)" in out  # count


def test_pnl_command_shows_realized_and_unrealized(temp_db):
    engine = _engine_stub(daily_pnl=125.0, positions=[
        {"ticker": "AAPL", "unrealized_pnl": 50.0},
        {"ticker": "TSLA", "unrealized_pnl": -25.0},
    ])
    out = dispatch("/pnl", engine)
    assert "P&amp;L" in out
    assert "$125.00" in out
    assert "$25.00" in out  # net unrealized


def test_last_command_returns_recent_trades(temp_db):
    """Each command is fed a real DB session via the temp_db fixture."""
    from backend.db import session_scope
    from backend.models.trade import Trade
    with session_scope() as s:
        s.add(Trade(
            ticker="AAPL", action="BUY_STOCK", quantity=10, price=200,
            strategy="rsi", signal_source="live_engine",
            status="closed", pnl=125.0,
        ))
    engine = _engine_stub()
    out = dispatch("/last", engine)
    assert "AAPL" in out
    assert "BUY_STOCK" in out


def test_last_n_command_respects_arg(temp_db):
    from backend.db import session_scope
    from backend.models.trade import Trade
    with session_scope() as s:
        for i in range(6):
            s.add(Trade(
                ticker=f"X{i}", action="BUY_STOCK",
                quantity=1, price=100, strategy="x",
                signal_source="live_engine", status="closed",
            ))
    engine = _engine_stub()
    out = dispatch("/last 2", engine)
    # We don't know order, just that two distinct tickers appear.
    assert sum(1 for tk in ("X0", "X1", "X2", "X3", "X4", "X5")
                if tk in out) == 2


def test_help_command(temp_db):
    engine = _engine_stub()
    out = dispatch("/help", engine)
    for cmd in ("/status", "/pause", "/resume",
                  "/positions", "/pnl", "/last", "/help"):
        assert cmd in out


def test_command_handler_swallows_exceptions(temp_db):
    """A handler that raises must still return a string, not propagate."""
    engine = MagicMock()
    engine.status = MagicMock(side_effect=RuntimeError("boom"))
    engine.executor.positions.side_effect = RuntimeError("boom")
    # /positions probes the executor; exception path returns empty list.
    out = dispatch("/positions", engine)
    assert isinstance(out, str)


def test_unknown_command_routes_to_hint(temp_db):
    engine = _engine_stub()
    out = dispatch("/whatever", engine)
    assert "Unknown command" in out
