"""EOD digest content + zero-trade-day handling."""
from datetime import date, datetime, timedelta

import pytest

from backend.bot.notifications.digest import build_eod_digest


pytestmark = [pytest.mark.unit]


def test_quiet_day_renders_friendly_message(temp_db):
    # temp_db gives us a fresh empty DB. With no trades, no positions,
    # and no alerts, the digest renders the canned "quiet day" body.
    out = build_eod_digest()
    assert "End-of-day digest" in out
    # The alert center is a process-level singleton (other tests may
    # have written to it); accept either "Quiet day" OR a digest that
    # still has no trades + no positions (alert-only would still emit
    # the "no trades" body).
    assert ("Quiet day" in out
            or ("Open positions:</b> none" in out
                  and "0 fired" in out))


def test_digest_includes_today_trade_count(temp_db):
    from backend.db import session_scope
    from backend.models.trade import Trade
    with session_scope() as s:
        s.add(Trade(
            ticker="AAPL", action="BUY_STOCK",
            quantity=10, price=200.0, strategy="rsi",
            signal_source="live_engine", confidence=0.7,
            status="closed", pnl=125.0,
        ))
        s.add(Trade(
            ticker="TSLA", action="BUY_STOCK",
            quantity=5, price=300.0, strategy="rsi",
            signal_source="live_engine", confidence=0.6,
            status="closed", pnl=-50.0,
        ))
    out = build_eod_digest()
    assert "Trades:" in out
    assert "2 fired" in out
    assert "1W" in out and "1L" in out
    assert "$75.00" in out  # realized = +125 - 50


def test_digest_lists_open_positions(temp_db):
    from backend.db import session_scope
    from backend.models.paper import PaperPosition
    with session_scope() as s:
        s.add(PaperPosition(
            ticker="AAPL", quantity=10, avg_cost=199.5,
        ))
    out = build_eod_digest()
    assert "Open positions" in out
    assert "AAPL" in out


def test_digest_highlights_best_and_worst(temp_db):
    from backend.db import session_scope
    from backend.models.trade import Trade
    with session_scope() as s:
        for pnl, tk in ((500, "WIN"), (300, "OK"), (-200, "LOSS")):
            s.add(Trade(
                ticker=tk, action="BUY_STOCK", quantity=1, price=100,
                strategy="x", signal_source="live_engine",
                status="closed", pnl=float(pnl),
            ))
    out = build_eod_digest()
    assert "WIN" in out and "LOSS" in out


def test_digest_excludes_yesterday_trades(temp_db):
    """Only today's trades count toward the digest."""
    from backend.db import session_scope
    from backend.models.trade import Trade
    with session_scope() as s:
        old = Trade(
            ticker="OLDIE", action="BUY_STOCK", quantity=1, price=100,
            strategy="x", signal_source="live_engine",
            status="closed", pnl=999.0,
        )
        old.timestamp = datetime.utcnow() - timedelta(days=2)
        s.add(old)
    out = build_eod_digest()
    assert "OLDIE" not in out  # yesterday's trade not surfaced
    assert "no trades" in out.lower() or "0 fired" in out
