"""MITS Phase 7 finishing pass — end-to-end opportunity firing test.

Validates the opportunistic execution loop closes end-to-end:

  * Synthetic panic regime context (SPY -2%, VIX 28+20%, breadth 0.18)
    forces the IntradayRegimeClassifier to label the tape capitulation.
  * Mock OpportunityBrain returns a high-conviction long_put hypothesis.
  * engine._run_opportunity_pass(...) produces an actual Trade row with:
      - signal_source == 'intraday_opportunistic'
      - opportunistic == 1
      - must_exit_by_eod == 1
      - detail_json['opportunity_hypothesis'] populated
      - detail_json['regime_at_entry'] populated
  * Sizing applies the 2.0x crisis multiplier (cap-aware).
  * Catalyst gate is BYPASSED for the shrink path on a non-earnings ticker
    + high conviction, but still ABSTAINS on short-DTE-into-earnings.
  * The intraday regime classifier persists an IntradayRegimeEvent on the
    state transition.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.bot.ai.opportunity_brain import OpportunityHypothesis
from backend.bot.engine import BotEngine
from backend.bot.executor import Executor
from backend.bot.market_data import MarketSnapshot
from backend.bot.regime.intraday_regime import (
    IntradayRegimeInputs,
    IntradayRegimeState,
    _classify_from_inputs,
)
from backend.db import session_scope
from backend.models.intraday_regime_event import IntradayRegimeEvent
from backend.models.trade import Trade


PANIC_TICKER = "QQQ"


def _panic_qqq_snapshot(ticker: str) -> MarketSnapshot:
    """QQQ snapshot the opportunistic gate + sizing path will see."""
    data = {
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
    }
    return MarketSnapshot(data=data, source_errors=[])


def _panic_inputs() -> IntradayRegimeInputs:
    """Synthetic panic inputs that the classifier labels capitulation."""
    return IntradayRegimeInputs(
        spy_pct_change_30m=-2.0,
        vix_spot=28.0,
        vix_1d_pct_change=22.0,
        put_call_ratio=1.42,
        breadth_ratio=0.18,
        prior_state="normal",
    )


@pytest.fixture()
def panic_engine(temp_db):
    """Engine instance pre-wired with a panicked tape + mocked Brain."""
    adapter = MagicMock()
    adapter.snapshot.side_effect = lambda t: _panic_qqq_snapshot(t)
    engine = BotEngine(executor=Executor(paper_mode=True),
                          market_data=adapter)

    # Force the classifier state directly so the test doesn't need to
    # wire every sector ETF + SPY snapshot. The persisted event row
    # mirrors what classify() would produce on a real panic tape.
    panic_state = _classify_from_inputs(_panic_inputs())
    panic_state.classified_at = "2026-06-06T14:30:00"
    engine._current_regime = panic_state.state
    engine._intraday_classifier._cache = panic_state
    engine._intraday_classifier._cache_at = 1e18  # never expires in test
    engine._intraday_classifier._last_state = panic_state.state

    # Mock the Opportunity Brain to return a high-conviction long_put
    # hypothesis on QQQ. The ``available`` property must be True so the
    # engine actually consults the brain.
    hypothesis = OpportunityHypothesis(
        ticker=PANIC_TICKER, direction="long_put",
        dte_bucket="0d", conviction=0.85,
        thesis=("QQQ breaking VWAP with VIX exploding and breadth "
                "collapsed. 0DTE put on bouncing strength offers convex "
                "payoff with controlled downside; exit before close."),
        notes="Invalidated if breadth thrust > 0.55 in next 15 min",
        regime_state=panic_state.state, from_cache=False,
    )
    engine._opportunity_brain = MagicMock()
    engine._opportunity_brain.available = True
    engine._opportunity_brain.analyze = MagicMock(return_value=hypothesis)
    return engine, panic_state


def _account(equity: float = 5000.0):
    return SimpleNamespace(
        portfolio_value=equity, buying_power=equity, cash=equity,
    )


def test_panic_regime_event_persists_on_transition(temp_db):
    """When the classifier flips normal → capitulation, an
    IntradayRegimeEvent row is written. Mirrors what run_cycle's
    top-of-cycle classify() emits on a real panic tape."""
    from backend.bot.regime.intraday_regime import _persist_event

    panic_state = _classify_from_inputs(_panic_inputs())
    assert panic_state.state == "capitulation"
    _persist_event("normal", panic_state)

    with session_scope() as s:
        rows = s.query(IntradayRegimeEvent).all()
        assert len(rows) == 1
        evt = rows[0]
        assert evt.new_state == "capitulation"
        assert evt.prior_state == "normal"
        assert evt.severity == "high"
        assert evt.spy_pct_change_30m == pytest.approx(-2.0)
        assert evt.vix_spot == pytest.approx(28.0)
        assert evt.breadth_ratio == pytest.approx(0.18)


def test_opportunity_pass_fires_a_real_trade(panic_engine, temp_db):
    """The headline test — the discretionary opportunism layer creates
    an ACTUAL Trade row with all the finishing-pass invariants set."""
    engine, _ = panic_engine

    events = engine._run_opportunity_pass(
        config={}, account=_account(equity=5000.0), held=set(),
    )

    # At least one event was produced, and the headline opportunity
    # event reached the "submitted" status (means _finalize_execution
    # successfully placed the order + _persist_trade wrote a Trade row).
    assert len(events) >= 1
    submitted = [e for e in events if e.get("status") == "submitted"]
    assert submitted, f"expected at least one submitted event, got: {events}"
    evt = submitted[0]
    assert evt.get("signal_source") == "intraday_opportunistic"
    assert evt.get("opportunistic") is True
    assert evt.get("must_exit_by_eod") is True
    assert evt.get("opportunity_hypothesis") is not None
    assert evt.get("regime_at_entry") is not None
    assert evt["regime_at_entry"]["state"] == "capitulation"

    # Confirm the persisted Trade row carries the finishing-pass marks.
    with session_scope() as s:
        trades = s.query(Trade).filter(Trade.ticker == PANIC_TICKER).all()
        assert len(trades) >= 1
        opp_trades = [t for t in trades if int(t.opportunistic or 0) == 1]
        assert opp_trades, "no Trade row has opportunistic=1"
        t = opp_trades[0]
        assert t.signal_source == "intraday_opportunistic"
        assert int(t.opportunistic) == 1
        assert int(t.must_exit_by_eod) == 1
        # detail_json carries the hypothesis + regime snapshot.
        assert t.detail_json
        import json as _json
        detail = _json.loads(t.detail_json)
        assert detail.get("opportunity_hypothesis") is not None
        assert detail.get("opportunity_hypothesis")["direction"] == "long_put"
        assert detail.get("opportunity_hypothesis")["conviction"] == pytest.approx(
            0.85, rel=1e-3,
        )
        assert detail.get("regime_at_entry") is not None
        assert detail["regime_at_entry"]["state"] == "capitulation"


def test_opportunity_pass_uses_2x_crisis_multiplier(panic_engine, temp_db):
    """High conviction on panic/capitulation regime → 2.0x sizing
    multiplier (the cap-aware path may scale down further, but the
    base multiplier MUST be 2.0). Confirms the sizing event surfaces
    the right number."""
    engine, _ = panic_engine

    events = engine._run_opportunity_pass(
        config={}, account=_account(equity=5000.0), held=set(),
    )
    submitted = [e for e in events if e.get("status") == "submitted"]
    assert submitted
    sizing = submitted[0].get("opportunistic_sizing") or {}
    # base multiplier is 2.0x; caps may truncate but a $5k account on a
    # $380 QQQ proposed-notional well within the 50%-of-equity single-
    # trade cap and 100%-of-equity daily cap, so 2.0x stays clean.
    assert sizing.get("multiplier") == pytest.approx(2.0, rel=1e-3)


def test_catalyst_shrink_bypass_on_high_conviction_crisis(panic_engine,
                                                                temp_db):
    """When regime != normal AND conviction ≥ bypass threshold, the
    catalyst-gate's ×0.5 shrink is skipped — the regime IS the
    opportunity. The event surfaces ``catalyst_shrink_skipped: True``."""
    engine, _ = panic_engine

    # Patch the catalyst_gate.check to simulate an FOMC-adjacent shrink
    # path. With high conviction on a crisis regime, the shrink must
    # NOT be applied to the sizing.
    from backend.bot.gates import catalyst_gate
    from backend.bot.gates.catalyst_gate import CatalystGateResult

    def _fake_check(ticker, *, instrument=None, dte=None, now=None):
        return CatalystGateResult(
            passes=True, conviction_multiplier=0.5,
            reason="catalyst_gate: simulated FOMC ≤24h (×0.50)",
            triggers=[{"kind": "fomc",
                          "datetime": "2026-06-06T18:00:00",
                          "hours_away": 1.0}],
        )

    real_check = catalyst_gate.check
    catalyst_gate.check = _fake_check
    try:
        events = engine._run_opportunity_pass(
            config={}, account=_account(equity=5000.0), held=set(),
        )
    finally:
        catalyst_gate.check = real_check

    submitted = [e for e in events if e.get("status") == "submitted"]
    assert submitted
    evt = submitted[0]
    assert evt.get("catalyst_shrink_skipped") is True
    # Sizing multiplier stays at 2.0× — the shrink was NOT applied.
    assert evt.get("opportunistic_sizing", {}).get("multiplier") == (
        pytest.approx(2.0, rel=1e-3)
    )


def test_catalyst_gate_short_dte_earnings_abstain_still_applies(panic_engine,
                                                                    temp_db):
    """Even on a crisis regime with high conviction, the hard ABSTAIN
    on short-DTE option INTO earnings ALWAYS wins. This is the operator's
    explicit invariant: regime override does NOT bypass the earnings
    short-DTE risk."""
    engine, _ = panic_engine

    from backend.bot.gates import catalyst_gate
    from backend.bot.gates.catalyst_gate import CatalystGateResult

    def _abstain_check(ticker, *, instrument=None, dte=None, now=None):
        return CatalystGateResult(
            passes=False, conviction_multiplier=0.0,
            reason=(f"catalyst_gate: {ticker} earnings 2026-06-07 (~1td) "
                       f"— option DTE={dte} ≤ 7 threshold. ABSTAIN."),
            triggers=[{"kind": "earnings", "date": "2026-06-07",
                          "trading_days_away": 1}],
        )

    real_check = catalyst_gate.check
    catalyst_gate.check = _abstain_check
    try:
        events = engine._run_opportunity_pass(
            config={}, account=_account(equity=5000.0), held=set(),
        )
    finally:
        catalyst_gate.check = real_check

    statuses = {e.get("status") for e in events}
    assert "catalyst_gate_abstain" in statuses
    assert "submitted" not in statuses

    # No Trade row was created on the ABSTAIN path.
    with session_scope() as s:
        trades = (s.query(Trade)
                    .filter(Trade.ticker == PANIC_TICKER)
                    .filter(Trade.opportunistic == 1)
                    .all())
        assert trades == []


def test_normal_regime_returns_empty(panic_engine, temp_db):
    """On normal regime the opportunistic pass is silent — statistical
    layer keeps the lights on, and zero discretionary events fire."""
    engine, _ = panic_engine
    engine._current_regime = "normal"
    events = engine._run_opportunity_pass(
        config={}, account=_account(equity=5000.0), held=set(),
    )
    assert events == []
