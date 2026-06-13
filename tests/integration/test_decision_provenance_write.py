"""MITS Phase 16.B — engine cycle writes decision_provenance (integration).

Drives a real engine cycle and asserts:
  • At least one decision_provenance row exists after the cycle
  • The row's consensus_json + chairman_memo_json + agent_outputs_json
    parse as valid JSON
  • Chairman memo carries kill_condition / structured_why /
    confidence_pct from 16.B
  • agent_outputs_json contains the 8 council agents' projections
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

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


def test_engine_cycle_writes_decision_provenance(temp_db):
    closes = [130.0 + (i % 3) * 0.5 for i in range(40)]
    _seed_bars("AAPL", closes)

    engine = _setup_engine("AAPL")
    engine.run_cycle()

    with session_scope() as s:
        rows = s.query(DecisionProvenance).all()
        snapshots = [r.to_dict() for r in rows]

    assert len(snapshots) >= 1, (
        "expected at least one decision_provenance row after a cycle "
        f"with a consensus-bearing event, got {len(snapshots)}"
    )
    row = snapshots[0]

    # Every consensus-bearing event must persist consensus_json. Other
    # *_json columns may be null on rejected paths (no policy_result
    # yet, no portfolio_context, etc) — test for parseability of the
    # ones that ARE present.
    assert row["consensus_json"], "consensus_json must be present"
    consensus = json.loads(row["consensus_json"])
    assert "stance" in consensus
    assert "votes" in consensus

    chairman = json.loads(row["chairman_memo_json"] or "{}")
    assert "decision" in chairman
    # 16.B extras MUST be present (even if None / empty)
    for key in (
        "kill_condition", "structured_why", "main_risk", "confidence_pct",
    ):
        assert key in chairman, f"chairman memo missing 16.B field {key!r}"

    # agent_outputs_json must hold the 8-agent projection list.
    outputs = json.loads(row["agent_outputs_json"] or "[]")
    assert isinstance(outputs, list)
    assert len(outputs) == 8, (
        f"expected 8 agent outputs (the registered council), got {len(outputs)}"
    )
    for out in outputs:
        for key in (
            "agent", "role", "stance", "confidence", "weight",
            "reasoning_type", "supporting_factors", "concerns",
        ):
            assert key in out, f"agent_output missing {key!r}: {out}"
        assert isinstance(out["confidence"], int)

    # agent_inputs_json must be a dict envelope (singular AgentInput).
    if row["agent_inputs_json"]:
        envelope = json.loads(row["agent_inputs_json"])
        assert isinstance(envelope, dict)
        assert envelope.get("ticker") == "AAPL"
        assert envelope.get("action")
        assert envelope.get("proposed_direction") in {"long", "short", "neutral"}
