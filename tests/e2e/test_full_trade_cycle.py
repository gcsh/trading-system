"""E2E: oversold-uptrend snapshot → RSIMeanReversion BUY_STOCK → trade logged."""
from unittest.mock import MagicMock

from backend.bot.engine import BotEngine
from backend.bot.executor import Executor
from backend.bot.market_data import MarketSnapshot
from backend.db import session_scope
from backend.models.config import load_config, save_config
from backend.models.trade import Trade


def _oversold_snapshot(_ticker):
    data = {
        "price": 130.0, "rsi": 22.0, "macd": -0.3, "macd_signal": -0.1,
        "macd_hist": -0.2, "prev_macd_hist": 0.1, "ma50": 145.0,
        "ma200": 120.0, "volume": 1_200_000, "avg_volume": 1_000_000,
        "iv_rank": 30, "adx": 18, "vix": 18, "news_score": 0.0,
        "earnings_days": 30, "pe_ratio": 22, "spy_trend": "neutral",
        "spy_adx": 18, "gap_pct": 0.0, "premarket_volume": 50_000,
        "shares_owned": 0, "position_value": 0, "portfolio_value": 25_000,
        "unrealized_gain_pct": 0.0, "high_52w": 160.0, "prev_close": 132.0,
        "vwap": 131.0, "momentum_5m": -0.1, "rsi_5m": 30,
        "market_trend": "neutral", "time_of_day": "11:00",
        "orb_high": 132.0, "orb_low": 129.0,
        "hist_earnings_move_avg": 0.05, "implied_move": 0.07,
        "has_catalyst": False, "earnings_today": False,
        "news_age_hours": 999, "range_3w_pct": 0.03,
    }
    return MarketSnapshot(data=data, source_errors=[])


def test_full_trade_cycle(temp_db):
    with session_scope() as session:
        cfg = load_config(session)
        cfg["strategy"] = "rsi_mean_reversion"
        cfg["tickers"] = ["AAPL"]
        cfg["trade_styles"] = ["swing"]
        cfg["signal_sources"] = {"technical": True}
        cfg["auto_execute"] = True
        # Bypass the engine's NYSE-closed gate so this test works in
        # CI / off-hours runs. The gate returns a single market_closed
        # HOLD event otherwise.
        cfg["force_run_when_closed"] = True
        save_config(session, cfg)

    adapter = MagicMock()
    adapter.snapshot.side_effect = _oversold_snapshot
    engine = BotEngine(executor=Executor(paper_mode=True), market_data=adapter)

    events = engine.run_cycle()
    assert len(events) == 1
    event = events[0]
    assert event["action"] == "BUY_STOCK"
    assert event["status"] == "submitted"
    assert event["paper"] is True

    with session_scope() as session:
        trades = session.query(Trade).all()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.ticker == "AAPL"
        assert trade.action == "BUY_STOCK"
        assert trade.paper == 1
        assert trade.strategy == "rsi_mean_reversion"
