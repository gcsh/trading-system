"""Engine integration: full cycle with the MarketDataAdapter mocked."""
from unittest.mock import MagicMock, patch

from backend.bot.engine import BotEngine
from backend.bot.executor import Executor
from backend.bot.market_data import MarketSnapshot


def _stub_snapshot(ticker):
    data = {
        "price": 150.0, "rsi": 52.0, "macd": 0.5, "macd_signal": 0.3,
        "macd_hist": 0.2, "prev_macd_hist": -0.1, "ma50": 140.0,
        "ma200": 120.0, "volume": 2_000_000, "avg_volume": 1_200_000,
        "iv_rank": 20, "adx": 28, "vix": 16, "news_score": 0.5,
        "earnings_days": 30, "pe_ratio": 22, "spy_trend": "bullish",
        "spy_adx": 28, "gap_pct": 0.0, "premarket_volume": 800_000,
        "shares_owned": 0, "position_value": 0, "portfolio_value": 100_000,
        "unrealized_gain_pct": 0.0, "high_52w": 158.0, "prev_close": 147.0,
        "vwap": 148.0, "momentum_5m": 0.0, "rsi_5m": 55,
        "market_trend": "bullish", "time_of_day": "10:30",
        "orb_high": 151.0, "orb_low": 148.0,
        "hist_earnings_move_avg": 0.09, "implied_move": 0.06,
        "has_catalyst": False, "earnings_today": False,
        "news_age_hours": 1.0, "range_3w_pct": 0.08,
    }
    return MarketSnapshot(data=data, source_errors=[])


def test_engine_runs_three_cycles_in_paper_mode(temp_db):
    adapter = MagicMock()
    adapter.snapshot.side_effect = lambda t: _stub_snapshot(t)
    engine = BotEngine(executor=Executor(paper_mode=True), market_data=adapter)
    for _ in range(3):
        events = engine.run_cycle()
        assert isinstance(events, list)
    assert engine.status.cycles == 3
    assert engine.status.last_cycle_at is not None


def test_engine_plan_for_session_populates_day_plan(temp_db):
    adapter = MagicMock()
    adapter.snapshot.side_effect = lambda t: _stub_snapshot(t)
    engine = BotEngine(executor=Executor(paper_mode=True), market_data=adapter)
    plan = engine.plan_for_session(["SPY", "AAPL"])
    assert plan.primary_strategy
    assert engine.status.day_plan is not None
    assert engine.status.market_regime in ("trending_up", "trending_down", "ranging", "volatile")
