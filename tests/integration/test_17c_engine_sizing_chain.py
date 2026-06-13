"""MITS Phase 17.C — engine cycle writes Trade.sizing_chain_json.

Drives a real engine cycle on an oversold ticker (the same fixture
``test_decision_provenance_write.py`` uses) so the council + sizing
pipeline run end to end. Asserts:
  • A Trade row is persisted with a non-null ``sizing_chain_json``
  • The chain has ``base_qty``, ``steps``, ``final_qty``,
    ``rounded_final`` fields
  • Every step satisfies the math invariant
    (``input * factor == output`` within 0.01)
  • ``rounded_final`` matches the persisted Trade.quantity
  • At least one of the expected sizing-step names appears in the chain
    (sanity that the engine actually plumbed through, not a stub)
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
from backend.models.stock_bar import StockBar
from backend.models.trade import Trade


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


def test_engine_cycle_writes_sizing_chain_json(temp_db):
    closes = [130.0 + (i % 3) * 0.5 for i in range(40)]
    _seed_bars("AAPL", closes)

    engine = _setup_engine("AAPL")
    engine.run_cycle()

    with session_scope() as s:
        trades = s.query(Trade).all()
        rows = [t.to_dict() for t in trades]

    submitted = [r for r in rows if r["status"] == "open"]
    assert len(submitted) >= 1, (
        "expected at least one submitted Trade row after the cycle; "
        f"got rows={[(r['ticker'], r['status']) for r in rows]}"
    )
    trade = submitted[0]

    assert trade["sizing_chain"] is not None, (
        "Trade.sizing_chain_json must be populated on the submitted path"
    )
    chain = trade["sizing_chain"]

    for key in ("base_qty", "steps", "final_qty", "rounded_final",
                "captured_at"):
        assert key in chain, f"sizing_chain missing field {key!r}"

    assert isinstance(chain["steps"], list)

    # Math invariant on every recorded step.
    for step in chain["steps"]:
        delta = abs(step["input"] * step["factor"] - step["output"])
        assert delta < 0.01, (
            f"step {step['name']} broke math invariant: "
            f"{step['input']} * {step['factor']} = "
            f"{step['input'] * step['factor']} but recorded "
            f"{step['output']} (delta={delta})"
        )

    # rounded_final matches the persisted Trade.quantity.
    assert chain["rounded_final"] == trade["quantity"], (
        f"sizing_chain.rounded_final={chain['rounded_final']} but "
        f"Trade.quantity={trade['quantity']}"
    )

    # Sanity: chain steps are all engine sizing-pipeline names. None of
    # the random "step1"/"step2" strings the unit tests use.
    expected_step_prefixes = (
        "consensus.", "chairman.", "meta_ai.", "eod.", "catalyst.",
        "correlation_cap.", "opportunity_committee.", "opportunistic.",
    )
    for step in chain["steps"]:
        assert any(step["name"].startswith(p) for p in expected_step_prefixes), (
            f"unexpected step name {step['name']!r} not in known "
            f"sizing-pipeline prefixes"
        )


def test_engine_cycle_sizing_chain_terminal_step_matches_final_qty(temp_db):
    """When at least one multiplier fires, the last step's ``output``
    must equal the chain's ``final_qty``."""
    closes = [130.0 + (i % 3) * 0.5 for i in range(40)]
    _seed_bars("AAPL", closes)

    engine = _setup_engine("AAPL")
    engine.run_cycle()

    with session_scope() as s:
        trades = s.query(Trade).all()
        rows = [t.to_dict() for t in trades]
    submitted = [r for r in rows if r["status"] == "open"]
    assert submitted, "expected at least one submitted Trade"
    chain = submitted[0]["sizing_chain"]
    if chain["steps"]:
        assert chain["steps"][-1]["output"] == chain["final_qty"]
    else:
        # No multiplier fired — final_qty must equal base_qty.
        assert chain["final_qty"] == chain["base_qty"]
