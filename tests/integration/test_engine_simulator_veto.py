"""MITS Phase 14.C — engine simulator veto (integration).

A real engine cycle whose candidate trade has a fabricated high-tail-risk
cohort. The simulator agent runs as part of ``run_consensus``, the
verdict carries a ``reject_reason``, and the engine short-circuits the
cycle with ``status="simulator_veto"``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from backend.bot.engine import BotEngine
from backend.bot.market_data import MarketSnapshot
from backend.bot.paper_executor import PaperExecutor
from backend.db import session_scope
from backend.models.config import load_config, save_config
from backend.models.stock_bar import StockBar


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
        # Don't gate via consensus abstain — we want the candidate to
        # reach the simulator veto.
        cfg["ai"] = {**(cfg.get("ai") or {}),
                     "consensus_abstain_enabled": False,
                     "brain_enabled": False,
                     "meta_enabled": False}
        save_config(session, cfg)
    adapter = MagicMock()
    adapter.snapshot.side_effect = _oversold
    return BotEngine(
        executor=PaperExecutor(starting_cash=10_000.0, price_fn=_flat_price),
        market_data=adapter,
    )


def test_simulator_veto_short_circuits_cycle(temp_db, monkeypatch):
    """Inject a high-tail-risk verdict via the simulator's analog path.
    The engine must surface ``status=simulator_veto`` for the candidate."""
    # Stub pgvector so the analog path falls into the cohort-cell
    # synthesis branch deterministically.
    import backend.bot.ai.vector_store as vs
    monkeypatch.setattr(vs, "embed", lambda text: [])
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: [])

    # Force the simulator to see a catastrophic cohort regardless of
    # what knowledge_evidence returns from the DB. The simplest hook
    # is to intercept ``agent_simulator``'s read of cohort cells —
    # patch load_knowledge_evidence to return a tail-risk cohort.
    from backend.bot import agent_context as agctx
    tail_cells = [
        {"sample_size": 100, "avg_return_pct": -0.60,
         "pattern": "rsi_oversold", "regime": "bullish",
         "vol_state": "normal"},
    ]
    monkeypatch.setattr(agctx, "load_knowledge_evidence",
                        lambda **kwargs: {
                            "cells": tail_cells,
                            "summary": "",
                            "most_similar_outcomes": [],
                        })

    # Seed bars for the candidate so the strategy can fire.
    closes = [130.0 + (i % 3) * 0.5 for i in range(40)]
    _seed_bars("AAPL", closes)

    engine = _setup_engine("AAPL")
    events = engine.run_cycle()

    aapl_events = [e for e in events
                   if (e.get("ticker") or "").upper() == "AAPL"]
    assert len(aapl_events) >= 1, (
        f"expected at least one AAPL event, got: "
        f"{[(e.get('ticker'), e.get('status')) for e in events]}"
    )
    # At least one AAPL event must be the simulator veto.
    veto_events = [e for e in aapl_events
                   if e.get("status") == "simulator_veto"]
    assert len(veto_events) == 1, (
        f"expected one simulator_veto event, got: "
        f"{[(e.get('ticker'), e.get('status'), e.get('reason')) for e in aapl_events]}"
    )
    event = veto_events[0]
    assert event["reason"].startswith("simulator_veto:")
    sv = event.get("simulator_verdict") or {}
    assert sv.get("reject_reason"), sv
    assert sv.get("p_max_loss", 0.0) > 0.30
