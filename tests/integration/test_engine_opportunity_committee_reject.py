"""MITS Phase 16.D — engine wires Opportunity Committee on the
opportunistic path.

Drives the same panic-tape fixture used by test_opportunity_end_to_end,
but engineers a hypothesis the committee will REJECT. Asserts:
  * the event status is 'opportunity_committee_reject'
  * the event carries an ``opportunity_committee`` block with the votes
  * no Trade row is persisted for the rejected hypothesis
  * a non-rejected high-conviction hypothesis still emits a Trade with
    the committee block riding along in detail_json
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.bot.ai.opportunity_brain import OpportunityHypothesis
from backend.bot.engine import BotEngine
from backend.bot.executor import Executor
from backend.bot.market_data import MarketSnapshot
from backend.bot.regime.intraday_regime import (
    IntradayRegimeInputs, _classify_from_inputs,
)
from backend.db import session_scope
from backend.models.trade import Trade


def _panic_snapshot(_t: str) -> MarketSnapshot:
    return MarketSnapshot(data={
        "price": 380.0, "rsi": 28.0, "macd": -1.2, "macd_signal": -0.6,
        "macd_hist": -0.6, "prev_macd_hist": -0.4, "ma50": 405.0,
        "ma200": 420.0, "volume": 90_000_000, "avg_volume": 50_000_000,
        "iv_rank": 78, "adx": 35, "vix": 28.0, "news_score": -0.8,
        "atr_30m": 4.0, "atr": 4.0,
        "earnings_days": 30, "pe_ratio": 22, "spy_trend": "bearish",
        "spy_adx": 30, "gap_pct": -1.4, "premarket_volume": 5_000_000,
        "shares_owned": 0, "position_value": 0,
        "portfolio_value": 5000.0, "unrealized_gain_pct": 0.0,
        "high_52w": 510.0, "prev_close": 388.0,
        "vwap": 384.0, "momentum_5m": -0.4, "rsi_5m": 24,
        "market_trend": "bearish", "time_of_day": "10:30",
        "orb_high": 392.0, "orb_low": 386.0,
        "intraday_30m_pct": -2.0, "intraday_60m_pct": -2.4,
        "put_call_ratio": 1.42, "breadth_ratio": 0.18,
        "vix_1d_pct": 22.0,
    }, source_errors=[])


def _panic_state():
    return _classify_from_inputs(IntradayRegimeInputs(
        spy_pct_change_30m=-2.0, vix_spot=28.0,
        vix_1d_pct_change=22.0, put_call_ratio=1.42,
        breadth_ratio=0.18, prior_state="normal",
    ))


def _account(equity: float = 5000.0):
    return SimpleNamespace(
        portfolio_value=equity, buying_power=equity, cash=equity,
        drawdown_pct=0.0,
    )


def _wire_engine(hypothesis: OpportunityHypothesis) -> BotEngine:
    adapter = MagicMock()
    adapter.snapshot.side_effect = lambda t: _panic_snapshot(t)
    engine = BotEngine(executor=Executor(paper_mode=True),
                          market_data=adapter)
    panic = _panic_state()
    engine._current_regime = panic.state
    engine._intraday_classifier._cache = panic
    engine._intraday_classifier._cache_at = 1e18
    engine._intraday_classifier._last_state = panic.state
    engine._opportunity_brain = MagicMock()
    engine._opportunity_brain.available = True
    engine._opportunity_brain.analyze = MagicMock(return_value=hypothesis)
    return engine


def test_committee_reject_short_circuits_before_trade(temp_db, monkeypatch):
    """High concurrency state → risk reviewer hard-rejects. No Trade
    row gets persisted; the event surfaces the committee block + REJECT
    status."""
    # Stub pgvector so the analog reviewer doesn't blow up — it'll still
    # reject on cohort_size < 3 but the test target is the engine wiring.
    import backend.bot.ai.vector_store as vs
    monkeypatch.setattr(vs, "embed", lambda text: [])
    monkeypatch.setattr(vs, "similarity_search",
                        lambda ns, vec, k=None: [])

    hypothesis = OpportunityHypothesis(
        ticker="QQQ", direction="long_put", dte_bucket="0d",
        conviction=0.85, regime_state="capitulation",
        thesis="forced reject case", notes="n",
        from_cache=False,
    )
    engine = _wire_engine(hypothesis)
    # Force the committee's risk reviewer to hard-reject by claiming a
    # full book of opportunistic trades already open.
    engine._opportunistic_concurrent_open = 10

    events = engine._run_opportunity_pass(
        config={}, account=_account(equity=5000.0), held=set(),
    )

    assert events, "expected at least one event"
    evt = events[-1]
    assert evt["status"] == "opportunity_committee_reject", (
        f"expected opportunity_committee_reject, got {evt.get('status')!r}"
    )
    assert "opportunity_committee" in evt
    comm = evt["opportunity_committee"]
    assert comm["recommendation"] == "REJECT"
    assert len(comm["votes"]) == 3
    # No Trade row should exist for QQQ.
    with session_scope() as s:
        trades = s.query(Trade).filter(Trade.ticker == "QQQ").all()
        # Opportunistic execution didn't happen, so there should be no
        # opportunistic Trade row. (statistical layer also doesn't run
        # here because we only invoked _run_opportunity_pass.)
        assert all(int(t.opportunistic or 0) == 0 for t in trades), (
            "no opportunistic Trade row should have been persisted "
            "after committee REJECT"
        )


def test_committee_block_rides_along_to_trade_detail(temp_db, monkeypatch):
    """High-conviction clean-tape hypothesis with the committee passing
    → Trade row gets persisted with opportunity_committee in detail_json.
    """
    # Stub pgvector + provide a strong cohort via the cohort-cell
    # fallback inside _analog_rollforward. The committee's own analog
    # reviewer still hits pgvector through retrieve_analogs — we provide
    # enough cohort to keep it from rejecting.
    import backend.bot.ai.vector_store as vs
    import backend.bot.corpus.analog_retrieval as ar
    from datetime import datetime
    from backend.bot.corpus.analog_retrieval import AnalogHit

    monkeypatch.setattr(vs, "embed", lambda text: [0.1] * 384)
    monkeypatch.setattr(
        vs, "similarity_search",
        lambda ns, vec, k=None: [
            SimpleNamespace(metadata={"date": "2025-01-01"}),
        ],
    )
    rows = [
        AnalogHit(
            observation_id=i, ticker="QQQ",
            timestamp=datetime(2025, 1, 1),
            distance=0.1, cosine=0.9,
            regime_label="capitulation",
            pattern_set=[],
            realized_return_pct=3.0 + i * 0.5,
            horizon="1d",
        )
        for i in range(12)
    ]
    monkeypatch.setattr(
        ar, "_outcomes_for_hits",
        lambda hits, *, ticker, horizon: rows if ticker == "QQQ" else [],
    )

    hypothesis = OpportunityHypothesis(
        ticker="QQQ", direction="long_put", dte_bucket="1d",
        conviction=0.85, regime_state="capitulation",
        thesis="QQQ breaking VWAP, vix elevated, controlled-risk put.",
        notes="",
        from_cache=False,
    )
    engine = _wire_engine(hypothesis)
    engine._opportunistic_concurrent_open = 0

    events = engine._run_opportunity_pass(
        config={}, account=_account(equity=5000.0), held=set(),
    )
    submitted = [e for e in events if e.get("status") == "submitted"]
    assert submitted, (
        f"expected a submitted opportunistic Trade, events were: "
        f"{[(e.get('status'), e.get('reason')) for e in events]}"
    )
    evt = submitted[0]
    assert "opportunity_committee" in evt
    assert evt["opportunity_committee"]["recommendation"] in (
        "EXECUTE", "SIZE_DOWN",
    )

    # Trade row carries committee in detail_json.
    with session_scope() as s:
        opp_trades = (
            s.query(Trade)
            .filter(Trade.ticker == "QQQ")
            .filter(Trade.opportunistic == 1)
            .all()
        )
        assert opp_trades, "no opportunistic Trade row persisted"
        import json
        detail = json.loads(opp_trades[0].detail_json)
        assert "opportunity_committee" in detail
        assert detail["opportunity_committee"]["recommendation"] in (
            "EXECUTE", "SIZE_DOWN",
        )
        assert len(detail["opportunity_committee"]["votes"]) == 3
