"""MITS Phase 16.B — provenance round-trip / replay (integration).

Pins:
  • A persisted DecisionProvenance row replays to a consensus whose
    stance matches and whose confidence drifts < 0.01 from the original.
  • Drift report flags both axes.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backend.bot.decision.replay import replay_consensus_from_provenance
from backend.bot.engine import BotEngine
from backend.bot.market_data import MarketSnapshot
from backend.bot.paper_executor import PaperExecutor
from backend.db import session_scope
from backend.models.config import load_config, save_config
from backend.models.decision_provenance import DecisionProvenance
from backend.models.stock_bar import StockBar


pytestmark = [pytest.mark.integration]


def _seed_bars(ticker: str, closes):
    base = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as s:
        for i, c in enumerate(closes):
            s.add(StockBar(
                ticker=ticker.upper(), interval="1d",
                bar_ts=base - timedelta(days=len(closes) - i),
                open=c, high=c, low=c, close=c, volume=1_000_000,
                source="test",
            ))


def _oversold(_ticker):
    return MarketSnapshot(data={
        "price": 130.0, "rsi": 22.0, "macd": -0.3, "macd_signal": -0.1,
        "macd_hist": -0.2, "prev_macd_hist": 0.1, "ma50": 145.0,
        "ma200": 120.0, "volume": 1_200_000, "avg_volume": 1_000_000,
        "iv_rank": 30, "adx": 18, "vix": 18, "news_score": 0.0,
        "earnings_days": 30, "pe_ratio": 22, "spy_trend": "neutral",
        "spy_adx": 18, "gap_pct": 0.0, "premarket_volume": 50_000,
        "shares_owned": 0, "position_value": 0, "portfolio_value": 10_000,
        "unrealized_gain_pct": 0.0, "high_52w": 160.0, "prev_close": 132.0,
        "vwap": 131.0, "momentum_5m": -0.1, "rsi_5m": 30,
        "market_trend": "neutral", "time_of_day": "11:00",
        "orb_high": 132.0, "orb_low": 129.0,
        "hist_earnings_move_avg": 0.05, "implied_move": 0.07,
        "has_catalyst": False, "earnings_today": False,
        "news_age_hours": 999, "range_3w_pct": 0.03,
    }, source_errors=[])


def _flat_price(_t):
    return 130.0


def _setup_engine(ticker: str):
    with session_scope() as session:
        cfg = load_config(session)
        cfg["strategy"] = "rsi_mean_reversion"
        cfg["tickers"] = [ticker]
        cfg["trade_styles"] = ["swing"]
        cfg["signal_sources"] = {"technical": True}
        cfg["auto_execute"] = True
        cfg["force_run_when_closed"] = True
        cfg["ai"] = {**(cfg.get("ai") or {}),
                     "brain_enabled": False,
                     "meta_enabled": False}
        save_config(session, cfg)
    adapter = MagicMock()
    adapter.snapshot.side_effect = _oversold
    return BotEngine(
        executor=PaperExecutor(starting_cash=10_000.0, price_fn=_flat_price),
        market_data=adapter,
    )


def test_provenance_replay_matches_persisted(temp_db):
    closes = [130.0 + (i % 3) * 0.5 for i in range(40)]
    _seed_bars("AAPL", closes)

    engine = _setup_engine("AAPL")
    engine.run_cycle()

    with session_scope() as s:
        row = s.query(DecisionProvenance).filter(
            DecisionProvenance.agent_outputs_json.isnot(None),
        ).order_by(DecisionProvenance.id.desc()).first()
        assert row is not None, (
            "no decision_provenance row with agent_outputs to replay"
        )
        prov_id = row.id
        persisted_consensus = json.loads(row.consensus_json or "{}")

    persisted_stance = persisted_consensus.get("stance")
    persisted_conf = float(persisted_consensus.get("confidence") or 0.0)

    result = replay_consensus_from_provenance(prov_id)

    assert result["persisted"]["stance"] == persisted_stance
    assert result["replayed"]["stance"] == persisted_stance, (
        f"stance drift: persisted={persisted_stance!r}, "
        f"replayed={result['replayed']['stance']!r}"
    )
    assert abs(result["replayed"]["confidence"] - persisted_conf) < 0.01, (
        f"confidence drift exceeds 0.01: persisted={persisted_conf}, "
        f"replayed={result['replayed']['confidence']}"
    )
    assert result["match"] is True
    assert result["drift"]["stance_drift"] is False
    assert result["drift"]["confidence_drift"] < 0.01
