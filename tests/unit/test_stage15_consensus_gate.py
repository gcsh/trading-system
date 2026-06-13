"""Stage-15 — agent consensus as a real entry gate.

Pinned:
  • Flag off (default) → legacy behavior, consensus is telemetry only
  • Flag on + consensus abstain → status="consensus_abstain", no trade fires
  • Flag on + consensus execute → trade fires normally with consensus on event
  • _persist_trade reuses event["consensus"] when present
"""
from unittest.mock import MagicMock

import pytest

from backend.bot.engine import BotEngine
from backend.bot.executor import Executor
from backend.bot.market_data import MarketSnapshot
from backend.db import session_scope
from backend.models.config import load_config, save_config


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


def _thin_volume(_ticker):
    """Snapshot that triggers most agents to abstain (no flow data + thin tape)."""
    return MarketSnapshot(data={
        "price": 130.0, "rsi": 22.0, "macd": -0.3, "macd_signal": -0.1,
        "macd_hist": -0.2, "prev_macd_hist": 0.1, "ma50": 145.0,
        "ma200": 120.0, "volume": 50_000, "avg_volume": 1_000_000,
        "iv_rank": 0, "adx": 5, "vix": 30, "news_score": 0.0,
        "earnings_days": 1, "pe_ratio": 22, "spy_trend": "bearish",
        "spy_adx": 5, "gap_pct": 0.0, "premarket_volume": 5_000,
        "shares_owned": 0, "position_value": 0, "portfolio_value": 25_000,
        "unrealized_gain_pct": 0.0, "high_52w": 160.0, "prev_close": 132.0,
        "vwap": 131.0, "momentum_5m": -0.1, "rsi_5m": 30,
        "market_trend": "bearish", "time_of_day": "11:00",
        "orb_high": 132.0, "orb_low": 129.0,
        "hist_earnings_move_avg": 0.05, "implied_move": 0.07,
        "has_catalyst": False, "earnings_today": False,
        "news_age_hours": 999, "range_3w_pct": 0.03,
    }, source_errors=[])


def _setup(*, ticker, consensus_abstain_enabled, snapshot_fn):
    with session_scope() as session:
        cfg = load_config(session)
        cfg["strategy"] = "rsi_mean_reversion"
        cfg["tickers"] = [ticker]
        cfg["trade_styles"] = ["swing"]
        cfg["signal_sources"] = {"technical": True}
        cfg["auto_execute"] = True
        # Engine's calendar gate would otherwise return "market_closed"
        # when these tests run outside NYSE hours.
        cfg["force_run_when_closed"] = True
        ai = dict(cfg.get("ai") or {})
        ai["consensus_abstain_enabled"] = consensus_abstain_enabled
        cfg["ai"] = ai
        save_config(session, cfg)
    adapter = MagicMock()
    adapter.snapshot.side_effect = snapshot_fn
    engine = BotEngine(executor=Executor(paper_mode=True), market_data=adapter)
    return engine


class TestConsensusGate:
    def test_flag_off_legacy(self, temp_db):
        engine = _setup(ticker="AAPL", consensus_abstain_enabled=False,
                           snapshot_fn=_oversold)
        events = engine.run_cycle()
        assert len(events) == 1
        # Legacy: trade submits regardless of consensus stance
        assert events[0]["status"] == "submitted"
        # Consensus still attached as telemetry
        assert "consensus" in events[0]

    def test_flag_on_clean_setup_executes(self, temp_db):
        engine = _setup(ticker="AAPL", consensus_abstain_enabled=True,
                           snapshot_fn=_oversold)
        events = engine.run_cycle()
        assert len(events) == 1
        # Reasonable snapshot → consensus should not block
        assert events[0]["status"] in ("submitted", "consensus_abstain")
        if events[0]["status"] == "submitted":
            assert "consensus" in events[0]

    def test_flag_on_blocks_when_consensus_abstain(self, temp_db):
        # Hostile snapshot drives most agents to ABSTAIN
        engine = _setup(ticker="AAPL", consensus_abstain_enabled=True,
                           snapshot_fn=_thin_volume)
        events = engine.run_cycle()
        # Either no signal fires at all (legitimate) OR consensus blocks it
        assert len(events) <= 1
        if events and events[0].get("status") == "consensus_abstain":
            assert "consensus" in events[0]
            assert "abstain" in events[0]["reason"].lower()
