"""MITS Phase 16.E — pre-fill decision rollback hook.

Exercises ``BotEngine._revalidate_decision_pre_fill`` directly so the
unit covers the four trigger paths without a full engine cycle:

  1. Flag OFF (default) — always returns None
  2. Regime trend flip (bullish → bearish) — returns "decision_stale"
  3. IV rank jump > 30 percentage points — returns "decision_stale"
  4. Max-correlation jump > 0.20 absolute — returns "decision_stale"
  5. No drift, flag ON — returns None

The helper rebuilds the RegimeVector from the current snapshot, so
each test patches ``build_regime_vector`` to return a deterministic
"current" view. The "original" view is stamped onto ``event`` exactly
how rule_consensus_exception writes it during the live cycle.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.bot.engine import BotEngine
from backend.bot.regime.vector import RegimeDimension, RegimeVector
from backend.bot.strategies.base import Action, Signal
from backend.config import TUNABLES
from datetime import datetime


def _engine() -> BotEngine:
    """Build a minimally-wired engine that the rollback helper can run
    on. The helper only reads ``self._intraday_classifier`` (passed
    through to build_regime_vector) and ``self.executor.positions()``
    (only on the correlation branch). Both are stubbed with MagicMocks
    so the four trigger paths can be exercised in isolation."""
    eng = BotEngine.__new__(BotEngine)
    eng._intraday_classifier = MagicMock()
    eng.executor = MagicMock()
    eng.executor.positions = MagicMock(return_value=[])
    return eng


def _signal(ticker: str = "AAPL") -> Signal:
    return Signal(
        action=Action.BUY_STOCK, ticker=ticker,
        confidence=0.7, reason="test", strategy="rsi_mean_reversion",
    )


def _dim(value, source="regime", health="green"):
    return RegimeDimension(
        value=value, freshness_seconds=0.0,
        source=source, health=health,
    )


def _rv(ticker, *, trend, iv_rank):
    return RegimeVector(
        ticker=ticker, as_of=datetime.utcnow(),
        trend=_dim(trend),
        volatility_state=_dim("normal"),
        iv_rank=_dim(iv_rank, source="iv_regime_cache"),
        iv_regime=_dim("mid"),
        intraday_regime=_dim("normal", source="intraday"),
        gamma_state=_dim({"regime": "neutral"}, source="gex"),
        macro_regime=_dim("growth", source="macro"),
        health="green",
    )


def test_rollback_off_returns_none(monkeypatch):
    """Default tunable is OFF — helper must short-circuit even with a
    massive trend flip stamped on the event."""
    monkeypatch.setattr(TUNABLES, "decision_rollback_enabled", False)
    engine = _engine()
    event = {
        "regime_vector": _rv("AAPL", trend="bullish", iv_rank=40.0).to_dict(),
    }
    result = engine._revalidate_decision_pre_fill(
        signal=_signal(), event=event, ticker="AAPL", data={},
    )
    assert result is None
    assert "rollback_reason" not in event


def test_rollback_on_no_drift_returns_none(monkeypatch):
    """Flag ON + identical regime vector → no drift → no abort."""
    monkeypatch.setattr(TUNABLES, "decision_rollback_enabled", True)
    engine = _engine()
    original = _rv("AAPL", trend="bullish", iv_rank=40.0)
    event = {"regime_vector": original.to_dict()}
    with patch(
        "backend.bot.regime.vector.build_regime_vector",
        return_value=original,
    ):
        result = engine._revalidate_decision_pre_fill(
            signal=_signal(), event=event, ticker="AAPL", data={},
        )
    assert result is None
    assert "rollback_reason" not in event


def test_rollback_on_trend_flip_aborts(monkeypatch):
    """Flag ON + trend flips bullish→bearish → returns decision_stale."""
    monkeypatch.setattr(TUNABLES, "decision_rollback_enabled", True)
    engine = _engine()
    original = _rv("AAPL", trend="bullish", iv_rank=40.0)
    current = _rv("AAPL", trend="bearish", iv_rank=40.0)
    event = {"regime_vector": original.to_dict()}
    with patch(
        "backend.bot.regime.vector.build_regime_vector",
        return_value=current,
    ):
        result = engine._revalidate_decision_pre_fill(
            signal=_signal(), event=event, ticker="AAPL", data={},
        )
    assert result == "decision_stale"
    assert "bullish" in event["rollback_reason"]
    assert "bearish" in event["rollback_reason"]


def test_rollback_on_iv_jump_aborts(monkeypatch):
    """Flag ON + IV rank jumps from 20 to 60 (40pp) → decision_stale."""
    monkeypatch.setattr(TUNABLES, "decision_rollback_enabled", True)
    engine = _engine()
    original = _rv("AAPL", trend="bullish", iv_rank=20.0)
    current = _rv("AAPL", trend="bullish", iv_rank=60.0)
    event = {"regime_vector": original.to_dict()}
    with patch(
        "backend.bot.regime.vector.build_regime_vector",
        return_value=current,
    ):
        result = engine._revalidate_decision_pre_fill(
            signal=_signal(), event=event, ticker="AAPL", data={},
        )
    assert result == "decision_stale"
    assert "IV rank" in event["rollback_reason"]
    assert "30pp" in event["rollback_reason"]


def test_rollback_on_iv_jump_under_threshold_passes(monkeypatch):
    """Flag ON + IV rank jumps 25pp (under the 30pp threshold) → no
    abort. Confirms the threshold is strictly > 30."""
    monkeypatch.setattr(TUNABLES, "decision_rollback_enabled", True)
    engine = _engine()
    original = _rv("AAPL", trend="bullish", iv_rank=20.0)
    current = _rv("AAPL", trend="bullish", iv_rank=45.0)
    event = {"regime_vector": original.to_dict()}
    with patch(
        "backend.bot.regime.vector.build_regime_vector",
        return_value=current,
    ):
        result = engine._revalidate_decision_pre_fill(
            signal=_signal(), event=event, ticker="AAPL", data={},
        )
    assert result is None


def test_rollback_choppy_trend_change_not_stale(monkeypatch):
    """A trend transition to/from 'choppy' (or 'unknown') is NOT a flip
    — only bullish↔bearish flips count."""
    monkeypatch.setattr(TUNABLES, "decision_rollback_enabled", True)
    engine = _engine()
    original = _rv("AAPL", trend="bullish", iv_rank=40.0)
    current = _rv("AAPL", trend="choppy", iv_rank=40.0)
    event = {"regime_vector": original.to_dict()}
    with patch(
        "backend.bot.regime.vector.build_regime_vector",
        return_value=current,
    ):
        result = engine._revalidate_decision_pre_fill(
            signal=_signal(), event=event, ticker="AAPL", data={},
        )
    assert result is None


def test_rollback_on_corr_jump_aborts(monkeypatch):
    """Flag ON + worst_rho jumps from 0.40 to 0.70 (0.30 absolute)
    → returns decision_stale on the correlation branch."""
    monkeypatch.setattr(TUNABLES, "decision_rollback_enabled", True)
    engine = _engine()
    # Identical RegimeVector — trend + IV branches must pass so we
    # reach the correlation branch.
    rv = _rv("AAPL", trend="bullish", iv_rank=40.0)
    event = {
        "regime_vector": rv.to_dict(),
        "correlation_cap": {
            "worst_rho": 0.40, "worst_peer": "MSFT",
            "candidate_direction": "LONG",
        },
    }
    # Patch check_correlation_cap so the current call returns 0.70.
    fake_curr = MagicMock(worst_rho=0.70)
    with patch(
        "backend.bot.regime.vector.build_regime_vector",
        return_value=rv,
    ), patch(
        "backend.bot.gates.correlation_cap_gate.check_correlation_cap",
        return_value=fake_curr,
    ), patch(
        "backend.bot.portfolio_intel.portfolio_context.build_portfolio_context",
        return_value=MagicMock(),
    ):
        result = engine._revalidate_decision_pre_fill(
            signal=_signal(), event=event, ticker="AAPL", data={},
        )
    assert result == "decision_stale"
    assert "correlation" in event["rollback_reason"]


def test_rollback_corr_jump_under_threshold_passes(monkeypatch):
    """Flag ON + worst_rho jumps 0.15 (under the 0.20 threshold) → no
    abort. Confirms strict > 0.20."""
    monkeypatch.setattr(TUNABLES, "decision_rollback_enabled", True)
    engine = _engine()
    rv = _rv("AAPL", trend="bullish", iv_rank=40.0)
    event = {
        "regime_vector": rv.to_dict(),
        "correlation_cap": {
            "worst_rho": 0.40, "worst_peer": "MSFT",
            "candidate_direction": "LONG",
        },
    }
    fake_curr = MagicMock(worst_rho=0.55)
    with patch(
        "backend.bot.regime.vector.build_regime_vector",
        return_value=rv,
    ), patch(
        "backend.bot.gates.correlation_cap_gate.check_correlation_cap",
        return_value=fake_curr,
    ), patch(
        "backend.bot.portfolio_intel.portfolio_context.build_portfolio_context",
        return_value=MagicMock(),
    ):
        result = engine._revalidate_decision_pre_fill(
            signal=_signal(), event=event, ticker="AAPL", data={},
        )
    assert result is None


def test_rollback_no_regime_vector_returns_none(monkeypatch):
    """Flag ON but event carries no persisted regime vector
    (pre-consensus block) → nothing to compare against → None."""
    monkeypatch.setattr(TUNABLES, "decision_rollback_enabled", True)
    engine = _engine()
    event = {}
    result = engine._revalidate_decision_pre_fill(
        signal=_signal(), event=event, ticker="AAPL", data={},
    )
    assert result is None


def test_decision_stale_rule_registered():
    """The sentinel rule MUST be registered as deferred so
    /policy/rules surfaces it but evaluate() never runs it."""
    from backend.bot.decision.policy import DecisionPolicy
    from backend.bot.decision.rules import _register_all
    policy = DecisionPolicy()
    _register_all(policy)
    rules = {r.name: r for r in policy.all_rules()}
    assert "decision_stale" in rules
    rule = rules["decision_stale"]
    assert rule.deferred is True
    assert rule.severity == "hard"
    assert rule.category == "data_quality"
