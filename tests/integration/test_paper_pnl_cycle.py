"""End-to-end proof that the paper bot actually realizes P&L.

Buy a position, move the price up, run a cycle, and assert the exit manager
closes it for a positive realized P&L — the exact thing that was broken
(trades opening and closing at the same price for $0).
"""
from unittest.mock import MagicMock

import pytest

from backend.bot.engine import BotEngine
from backend.bot.market_data import MarketSnapshot
from backend.bot.paper_executor import PaperExecutor
from backend.db import session_scope
from backend.models.config import load_config, save_config
from backend.models.trade import Trade


class PricedAdapter:
    """MarketDataAdapter stand-in with a controllable price per ticker."""

    def __init__(self, price):
        self.price = price

    def snapshot(self, ticker):
        data = {
            "price": self.price, "rsi": 45.0, "macd": 0.5, "macd_signal": 0.3,
            "macd_hist": 0.2, "prev_macd_hist": -0.1, "ma50": 90.0, "ma200": 80.0,
            "volume": 2_000_000, "avg_volume": 1_000_000, "iv_rank": 20, "adx": 28,
            "vix": 16, "news_score": 0.5, "earnings_days": 30, "pe_ratio": 22,
            "spy_trend": "bullish", "spy_adx": 28, "gap_pct": 0.0,
            "premarket_volume": 0, "shares_owned": 0, "position_value": 0,
            "portfolio_value": 0, "unrealized_gain_pct": 0.0, "high_52w": 200.0,
            "prev_close": self.price, "vwap": self.price, "momentum_5m": 0.0,
            "rsi_5m": 50, "market_trend": "bullish", "time_of_day": "11:00",
            "orb_high": self.price, "orb_low": self.price * 0.99,
            "hist_earnings_move_avg": 0.05, "implied_move": 0.06,
            "has_catalyst": False, "earnings_today": False, "news_age_hours": 999,
            "range_3w_pct": 0.05,
        }
        return MarketSnapshot(data=data, source_errors=[])


def _seed_config():
    with session_scope() as session:
        cfg = load_config(session)
        cfg["strategy"] = "rsi_mean_reversion"
        cfg["tickers"] = ["AAPL"]
        cfg["trade_styles"] = ["swing"]
        cfg["auto_execute"] = True
        cfg["min_confidence"] = 0.3
        cfg["broker"] = "local_paper"
        cfg["risk"] = {
            "max_position_size_usd": 500,
            "max_open_positions": 5,
            "daily_loss_limit_usd": 1000,
            "stop_loss_pct": 5,
            "take_profit_pct": 10,
            "max_cash_usage_pct": 100,
        }
        save_config(session, cfg)


def test_take_profit_realizes_positive_pnl(temp_db):
    _seed_config()
    # Buy AAPL at $100 with a deterministic price function on the executor.
    price_box = {"p": 100.0}
    executor = PaperExecutor(starting_cash=1000.0, price_fn=lambda t: price_box["p"])

    # Manually open a position at $100 (simulate a prior entry).
    buy = executor.place_stock_order("AAPL", "BUY", 4)  # $400 of AAPL
    assert buy.success
    pos = executor.open_position("AAPL")
    assert pos and abs(pos["quantity"] - 4) < 1e-6

    # Price jumps 12% — above the 10% take-profit threshold.
    price_box["p"] = 112.0

    engine = BotEngine(executor=executor, market_data=PricedAdapter(112.0))
    events = engine.run_cycle()

    # The exit manager should have closed AAPL for a profit.
    sell_events = [e for e in events if e["action"] == "SELL_STOCK" and e["status"] == "submitted"]
    assert sell_events, f"expected a take-profit sell, got {[(e['ticker'], e['action'], e['status']) for e in events]}"
    assert sell_events[0]["pnl"] is not None
    assert sell_events[0]["pnl"] > 0  # (112-100)*4 = +48

    # Position is gone, cash is back up, realized P&L recorded.
    assert executor.open_position("AAPL") is None
    state = executor.get_account_state()
    assert state["realized_pnl"] > 0
    assert state["cash"] > 1000.0  # started 1000, sold higher than bought

    with session_scope() as session:
        closed = session.query(Trade).filter(Trade.status == "closed").all()
        assert any(t.pnl and t.pnl > 0 for t in closed)


def test_stop_loss_realizes_negative_pnl(temp_db):
    _seed_config()
    price_box = {"p": 100.0}
    executor = PaperExecutor(starting_cash=1000.0, price_fn=lambda t: price_box["p"])
    executor.place_stock_order("AAPL", "BUY", 4)

    # Price drops 8% — below the 5% stop-loss threshold.
    price_box["p"] = 92.0
    engine = BotEngine(executor=executor, market_data=PricedAdapter(92.0))
    events = engine.run_cycle()

    sell_events = [e for e in events if e["action"] == "SELL_STOCK" and e["status"] == "submitted"]
    assert sell_events
    assert sell_events[0]["pnl"] < 0  # (92-100)*4 = -32
    assert executor.open_position("AAPL") is None


def test_no_pyramiding_when_already_held(temp_db):
    _seed_config()
    price_box = {"p": 100.0}
    executor = PaperExecutor(starting_cash=1000.0, price_fn=lambda t: price_box["p"])
    executor.place_stock_order("AAPL", "BUY", 2)

    # Price unchanged (no exit). A BUY signal should be skipped as already_held.
    engine = BotEngine(executor=executor, market_data=PricedAdapter(100.0))
    events = engine.run_cycle()
    held_events = [e for e in events if e.get("status") == "already_held"]
    # AAPL is held; if the strategy emitted BUY it must be skipped, not re-bought.
    buys = [e for e in events if e["action"].startswith("BUY") and e["status"] == "submitted"]
    assert not buys, "should not pyramid into an already-held ticker"
