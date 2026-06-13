"""MITS Phase 16.A — every legacy hidden gate now produces an explicit
BlockingFactor instead of a silent debug log.

Each test injects the same kind of exception the legacy code used to
swallow (analytics raise, meta audit raise, drift probe raise, etc.)
into ``DecisionPolicy.evaluate`` and asserts the rule fires.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.bot.decision.policy import DecisionPolicy, PolicyContext
from backend.bot.decision.rules import _register_all
from backend.bot.strategies.base import Action, Signal


def _signal():
    return Signal(
        action=Action.BUY_STOCK, ticker="AAPL", confidence=0.9,
        reason="test", strategy="s",
    )


def _ctx(**over) -> PolicyContext:
    ctx = PolicyContext(
        ticker="AAPL", signal=_signal(),
        event={"timestamp": "2026-06-11T15:00:00",
                "ticker": "AAPL", "reason": "test"},
        data={"price": 100.0, "iv_rank": 30.0},
        analytics_cfg={"enabled": True}, ai_config={},
        config={"force_run_when_closed": True, "min_confidence": 0.4},
        kill_active=False, portfolio_risk_dict=None,
        eod_bias_map={}, brain_cooldown={}, use_brain=False,
        cycle_id="cycle-1",
    )
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


def _policy() -> DecisionPolicy:
    p = DecisionPolicy()
    _register_all(p)
    return p


def test_analytics_failure_surfaces_as_blocking_factor():
    """Hidden gate #1 — engine.py L2343-47 try/except around
    analytics.evaluate."""
    engine = MagicMock()
    engine.evaluate.side_effect = RuntimeError("yfinance 503")
    ctx = _ctx(analytics_engine=engine,
                  risk_manager=MagicMock(),
                  account=SimpleNamespace(portfolio_value=10000.0),
                  executor=MagicMock(),
                  meta_engine=SimpleNamespace(available=False),
                  intraday_classifier=MagicMock())
    result = _policy().evaluate(ctx)
    assert result.eligible is False
    blocked_rules = {b.rule for b in result.blocking_factors}
    assert "analytics_build_failed" in blocked_rules
    # The headline blocker is the first hard rule by registration order.
    head = result.headline_blocker()
    assert head.rule == "analytics_build_failed"
    assert "yfinance" in head.evidence.get("exception", "")


def test_drift_probe_failure_surfaces():
    """Hidden gate #2 — engine.py L2414-18 try/except around
    drift.auto_halt.is_halted."""
    # Stub analytics to succeed so we reach the drift rule.
    analytics_result = SimpleNamespace(
        rank=SimpleNamespace(grade="A"),
        regime=SimpleNamespace(trend="trending_up", label="bull",
                                    volatility="normal"),
        probability=SimpleNamespace(probability=0.65, expected_move=0.02),
        to_dict=lambda: {"regime": {"trend": "trending_up"}},
    )
    analytics_engine = MagicMock()
    analytics_engine.evaluate.return_value = analytics_result

    import backend.bot.drift.auto_halt as _dh

    def _boom(_s):
        raise RuntimeError("drift store offline")

    orig = _dh.is_halted
    _dh.is_halted = _boom
    try:
        ctx = _ctx(
            analytics_engine=analytics_engine,
            risk_manager=MagicMock(),
            account=SimpleNamespace(portfolio_value=10000.0),
            executor=MagicMock(),
            meta_engine=SimpleNamespace(available=False),
            intraday_classifier=MagicMock(),
        )
        # Skip the abstain rule by giving it a passing decision.
        result = _policy().evaluate(ctx)
    finally:
        _dh.is_halted = orig

    blocked_rules = {b.rule for b in result.blocking_factors}
    assert "drift_check_failed" in blocked_rules


def test_consensus_failure_surfaces(monkeypatch):
    """Hidden gate #3 — engine.py L2722-27 try/except around
    run_consensus."""
    analytics_result = SimpleNamespace(
        rank=SimpleNamespace(grade="A"),
        regime=SimpleNamespace(trend="trending_up", label="bull",
                                    volatility="normal"),
        probability=SimpleNamespace(probability=0.65, expected_move=0.02),
        to_dict=lambda: {"regime": {"trend": "trending_up",
                                          "volatility": "normal"}},
    )
    analytics_engine = MagicMock()
    analytics_engine.evaluate.return_value = analytics_result

    def _boom(*a, **kw):
        raise RuntimeError("council import broken")
    monkeypatch.setattr("backend.bot.agents.run_consensus", _boom)
    monkeypatch.setattr(
        "backend.bot.drift.auto_halt.is_halted", lambda _s: False,
    )
    monkeypatch.setattr(
        "backend.bot.analytics.gate_by_grade", lambda r, g: True,
    )

    ctx = _ctx(
        analytics_engine=analytics_engine,
        risk_manager=MagicMock(),
        account=SimpleNamespace(portfolio_value=10000.0),
        executor=MagicMock(),
        meta_engine=SimpleNamespace(available=False),
        intraday_classifier=MagicMock(),
    )
    result = _policy().evaluate(ctx)
    blocked_rules = {b.rule for b in result.blocking_factors}
    assert "consensus_exception" in blocked_rules


def test_meta_offline_surfaces_as_soft_penalty(monkeypatch):
    """Hidden gate #4 — engine.py L2473-77 try/except around meta.audit.
    Meta failure must become a soft (5%) sizing-penalty BlockingFactor,
    not a silent debug log."""
    analytics_result = SimpleNamespace(
        rank=SimpleNamespace(grade="A"),
        regime=SimpleNamespace(trend="trending_up", label="bull",
                                    volatility="normal"),
        probability=SimpleNamespace(probability=0.65, expected_move=0.02),
        to_dict=lambda: {"regime": {"trend": "trending_up",
                                          "volatility": "normal"}},
    )
    analytics_engine = MagicMock()
    analytics_engine.evaluate.return_value = analytics_result

    meta_engine = MagicMock()
    meta_engine.available = True
    meta_engine.audit.side_effect = RuntimeError("anthropic 401")

    monkeypatch.setattr(
        "backend.bot.drift.auto_halt.is_halted", lambda _s: False,
    )
    monkeypatch.setattr(
        "backend.bot.analytics.gate_by_grade", lambda r, g: True,
    )

    # Stub council so we get past the consensus_exception rule.
    consensus_obj = SimpleNamespace(
        recommendation="buy", abstain_count=0, votes=[],
        simulator_verdict={},
        to_dict=lambda: {"chairman_report": {}},
    )
    monkeypatch.setattr(
        "backend.bot.agents.run_consensus",
        lambda *a, **kw: consensus_obj,
    )
    monkeypatch.setattr(
        "backend.bot.regime.vector.build_regime_vector",
        lambda **kw: SimpleNamespace(to_dict=lambda: {}),
    )
    # Stub portfolio context + correlation cap so we make it through.
    monkeypatch.setattr(
        "backend.bot.portfolio_intel.portfolio_context.build_portfolio_context",
        lambda **kw: SimpleNamespace(to_dict=lambda: {}),
    )
    monkeypatch.setattr(
        "backend.bot.gates.correlation_cap_gate.check_correlation_cap",
        lambda **kw: SimpleNamespace(
            blocked=False, reason="",
            to_dict=lambda: {"blocked": False},
        ),
    )
    # Risk approves.
    risk = MagicMock()
    risk.evaluate.return_value = SimpleNamespace(
        approved=True, reason="ok", quantity=1.0,
    )

    ctx = _ctx(
        analytics_engine=analytics_engine,
        risk_manager=risk,
        account=SimpleNamespace(portfolio_value=10000.0),
        executor=SimpleNamespace(positions=lambda: []),
        meta_engine=meta_engine,
        intraday_classifier=MagicMock(),
    )
    ctx.ai_config = {"meta_enabled": True}

    result = _policy().evaluate(ctx)
    # Hard rules pass — eligibility holds — but the soft meta_ai_offline
    # surfaces in blocking_factors.
    assert result.eligible is True
    soft_rules = {b.rule for b in result.blocking_factors
                  if b.severity == "soft"}
    assert "meta_ai_offline" in soft_rules
    assert result.soft_penalties_total_pct >= 5.0
