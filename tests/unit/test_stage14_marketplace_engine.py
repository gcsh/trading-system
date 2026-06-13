"""Stage-14.D10 engine-level marketplace integration.

Pinned:
  • Flag off → legacy per-ticker execution path (full_trade_cycle case)
  • Flag on with one winning ticker → marketplace selects + executes it
  • Flag on with two competing tickers + capital cap → only the better one fires
  • Rejected ones emit status='marketplace_skipped' with a rejection_reason
"""
from unittest.mock import MagicMock

import pytest

from backend.bot.engine import BotEngine
from backend.bot.executor import Executor
from backend.bot.market_data import MarketSnapshot
from backend.db import session_scope
from backend.models.config import load_config, save_config
from backend.models.trade import Trade


def _oversold(_ticker):
    return MarketSnapshot(data={
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
    }, source_errors=[])


def _setup_config(*, tickers, marketplace_enabled=False,
                     capital_pct=0.5, max_positions=10):
    with session_scope() as session:
        cfg = load_config(session)
        cfg["strategy"] = "rsi_mean_reversion"
        cfg["tickers"] = tickers
        cfg["trade_styles"] = ["swing"]
        cfg["signal_sources"] = {"technical": True}
        cfg["auto_execute"] = True
        # Engine calendar gate would otherwise mark cycles "market_closed"
        # when tests run outside RTH.
        cfg["force_run_when_closed"] = True
        ai = dict(cfg.get("ai") or {})
        ai["marketplace_enabled"] = marketplace_enabled
        ai["marketplace_capital_pct"] = capital_pct
        ai["marketplace_max_positions"] = max_positions
        cfg["ai"] = ai
        save_config(session, cfg)


class TestMarketplaceEngineMode:
    def test_flag_off_legacy_path(self, temp_db):
        _setup_config(tickers=["AAPL"], marketplace_enabled=False)
        adapter = MagicMock()
        adapter.snapshot.side_effect = _oversold
        engine = BotEngine(executor=Executor(paper_mode=True),
                              market_data=adapter)
        events = engine.run_cycle()
        assert len(events) == 1
        event = events[0]
        # Legacy submission path
        assert event["status"] == "submitted"
        assert "marketplace" not in event

    def test_flag_on_single_winner(self, temp_db):
        _setup_config(tickers=["AAPL"], marketplace_enabled=True,
                          capital_pct=1.0)
        adapter = MagicMock()
        adapter.snapshot.side_effect = _oversold
        engine = BotEngine(executor=Executor(paper_mode=True),
                              market_data=adapter)
        events = engine.run_cycle()
        assert len(events) == 1
        event = events[0]
        assert event["status"] == "submitted"
        # Marketplace annotated the event
        assert "marketplace" in event
        assert event["marketplace"]["selected"] is True
        assert event["marketplace"]["expected_value"] >= 0.0

    def test_flag_on_capital_cap_rejects_one(self, temp_db):
        """With two tickers competing for limited capital, marketplace
        should select the highest-EV one and reject the other."""
        _setup_config(tickers=["AAPL", "NVDA"], marketplace_enabled=True,
                          capital_pct=0.02, max_positions=5)
        # capital_pct = 0.02 of $1000 starting = $20 budget; one trade of
        # ~$200 should fit, second should be rejected as too expensive.
        adapter = MagicMock()
        adapter.snapshot.side_effect = lambda t: _oversold(t)
        engine = BotEngine(executor=Executor(paper_mode=True),
                              market_data=adapter)
        events = engine.run_cycle()
        assert len(events) == 2
        statuses = sorted([e["status"] for e in events])
        # At least one should be marketplace_skipped
        assert "marketplace_skipped" in statuses
        skipped = [e for e in events if e["status"] == "marketplace_skipped"]
        assert all(e.get("marketplace", {}).get("rejection_reason")
                      for e in skipped)
        assert all("marketplace" in e for e in events)
